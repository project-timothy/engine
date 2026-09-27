"""Crash consistency: the two stores converge and a half-persisted run
never replays (#136).

Three diseases from the 2026-08-20 assessment:
- append_event commits SQLite before the JSONL line; a crash in between
  diverges the stores permanently (nothing ever backfills the line).
- One torn trailing JSONL line (crash mid-append) makes every
  event-reading key() raise, wedging all runs.
- The runner records the run (commit) before events and approvals exist;
  a crash in between replays a result whose cards were never created.
"""

from __future__ import annotations

import json

import pytest

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.ledger.event_log import EVENT_LOG_FILENAME


def _seeded(tmp_path):
    root = tmp_path / "led"
    with Ledger.open(root) as ledger:
        stored = ledger.record_run(
            idempotency_key="run-1",
            tenant="demo",
            agent="ap",
            job="j",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="s",
        )
        for i in range(3):
            ledger.append_event(
                idempotency_key=f"evt-{i}",
                run_id=stored.id,
                tenant="demo",
                agent="ap",
                event_type="t.e",
                payload={"i": i},
            )
    return root


def test_torn_trailing_line_tolerated_and_repaired(tmp_path):
    root = _seeded(tmp_path)
    log = root / EVENT_LOG_FILENAME
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"idempotency_key": "evt-torn", "pay')  # crash mid-append

    with Ledger.open(root, repair_event_log=True) as ledger:
        records = ledger.read_event_log()

    assert [r["idempotency_key"] for r in records] == ["evt-0", "evt-1", "evt-2"]
    # the torn fragment is gone from the file and every line parses
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)


def test_missing_jsonl_tail_backfills_from_sqlite(tmp_path):
    root = _seeded(tmp_path)
    log = root / EVENT_LOG_FILENAME
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")  # crash before append

    with Ledger.open(root, repair_event_log=True) as ledger:
        records = ledger.read_event_log()

    assert [r["idempotency_key"] for r in records] == ["evt-0", "evt-1", "evt-2"]
    restored = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert restored["idempotency_key"] == "evt-2"
    assert restored["payload"] == {"i": 2}


def test_reader_tolerates_a_torn_final_line_without_repair(tmp_path):
    root = _seeded(tmp_path)
    log = root / EVENT_LOG_FILENAME
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"torn')

    with Ledger.open(root) as ledger:  # no repair (e.g. the auditor's read)
        records = ledger.read_event_log()

    assert [r["idempotency_key"] for r in records] == ["evt-0", "evt-1", "evt-2"]


def test_half_persisted_run_is_rolled_back_and_re_executes(tmp_path, monkeypatch):
    """Crash between record_run and the approval writes: the stored run row
    must not survive alone, or every later invocation replays a result
    whose cards never existed."""
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "scan.pdf").write_bytes(b"%PDF no text")
    (landing / "scan.pdf.extract.json").write_text(
        '{"doc_type": "unknown", "confidence": 0.0, "needs_ocr": true}'
    )
    params = {"landing_dir": str(landing), "extractor": "fixture"}

    real = Ledger.enqueue_approval

    def crash(self, **kwargs):
        raise RuntimeError("simulated crash mid-persistence")

    monkeypatch.setattr(Ledger, "enqueue_approval", crash)
    with pytest.raises(RuntimeError):
        run("demo", "ap", "intake", shadow=True, params=params, ledger_dir=tmp_path)
    monkeypatch.setattr(Ledger, "enqueue_approval", real)

    result = run("demo", "ap", "intake", shadow=True, params=params, ledger_dir=tmp_path)

    assert result.status == "needs_approval"  # executed fresh, never a replay
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        pending = ledger.list_approvals("demo", status="pending")
    assert any(p["action_type"] == "ap.review_needs_ocr" for p in pending)
