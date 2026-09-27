"""A transport-flagged document re-fires AP intake on a later run.

2026-09-04: the Claude Code CLI auto-updated overnight and every model call
failed; AP intake flagged two real customer documents as ``transport_error``
(docs/lessons.md, "Transient is not terminal"). A transport failure is a verdict about the
pipe, not the document, but the intake key covered only the landing files,
params, extractor, vendors, and config, none of which change when the
transport recovers, so the same landing state replayed as a noop forever and
the two files sat flagged until an operator re-fired intake by hand with a
different ``--param since``.

The contract these evals enforce (the expenses match verify-pass shape,
PR #171):

- ``transport_error`` and ``timeout`` flags are retryable: while such a flag
  names a file that is still a landing candidate, the tenant-local day is an
  intake-key input. The flag landing is itself new input state, so the run
  right after it retries once; further same-day re-runs replay; each later
  day's run extracts the file again as if new.
- ``oversize`` and ``bad_reply`` are verdicts about the document: final, never
  retried, and they never put the day into the key.
- The key changes exactly when the retry day changes AND a retryable flag
  exists, not otherwise.
"""

from __future__ import annotations

from pathlib import Path

from conftest import minimal_pdf
from core.agents.ap import jobs as ap_jobs
from core.agents.ap.extraction import ExtractionError, FixtureExtractor
from core.agents.ap.schema import ExtractedDocument


class _FlakyExtractor:
    """Wraps the fixture extractor; raises a scripted ExtractionError for a
    named file until its failure budget is spent. Counts calls per file."""

    def __init__(self, *, fail: dict[str, tuple[int, str, bool]]) -> None:
        # name -> (times to fail, cause, transient)
        self._fail = dict(fail)
        self._inner = FixtureExtractor()
        self.calls: dict[str, int] = {}

    def extract(self, path: Path) -> ExtractedDocument:
        self.calls[path.name] = self.calls.get(path.name, 0) + 1
        budget = self._fail.get(path.name)
        if budget and self.calls[path.name] <= budget[0]:
            _, cause, transient = budget
            raise ExtractionError(f"simulated {cause}", cause=cause, transient=transient)
        return self._inner.extract(path)


def _make_landing(tmp_path: Path) -> Path:
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "NAI_PO.pdf").write_bytes(minimal_pdf("customer document"))
    (landing / "NAI_PO.pdf.extract.json").write_text(
        '{"doc_type": "invoice", "vendor_name": "Alpha Parts", "invoice_number": "A-1",'
        ' "amount": "10.00", "invoice_date": "2026-09-01", "confidence": 0.95}',
        encoding="utf-8",
    )
    return landing


def _run_intake(landing: Path, ledger_dir: Path):
    from core.engine.runner import run

    return run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=ledger_dir,
    )


def _events(ledger_dir: Path, event_type: str) -> list[dict]:
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e["event_type"] == event_type]


def _use(monkeypatch, extractor: _FlakyExtractor) -> None:
    monkeypatch.setattr(ap_jobs, "_extractor", lambda ctx: extractor)


def test_transport_flag_retries_next_day_and_records_the_invoice(tmp_path, monkeypatch):
    """The 2026-09-04 shape: the transport fails, the file is flagged. The
    flag itself is new input state, so the very next run retries once (the
    #171 "id landing is new input state: one retry" shape); a further
    same-day re-run replays; the next day's run extracts the file again and
    records it. Never a hand re-fire with a different ``since``."""
    landing = _make_landing(tmp_path)
    extractor = _FlakyExtractor(fail={"NAI_PO.pdf": (2, "transport_error", True)})
    _use(monkeypatch, extractor)
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-04")

    first = _run_intake(landing, tmp_path / "data")

    assert first.status == "ok"
    assert "FLAGGED 1" in first.summary
    (flag,) = _events(tmp_path / "data", "ap.intake.flagged")
    assert flag["payload"]["cause"] == "transport_error"
    assert extractor.calls["NAI_PO.pdf"] == 1

    right_after = _run_intake(landing, tmp_path / "data")
    assert right_after.status == "ok"  # the flag landing is new input state: one retry
    assert "FLAGGED 1" in right_after.summary
    assert extractor.calls["NAI_PO.pdf"] == 2

    same_day = _run_intake(landing, tmp_path / "data")
    assert same_day.status == "noop"  # nothing changed since: a replay, not a third dial
    assert extractor.calls["NAI_PO.pdf"] == 2

    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-05")
    next_day = _run_intake(landing, tmp_path / "data")

    assert next_day.status == "ok"
    assert extractor.calls["NAI_PO.pdf"] == 3  # extracted again as if new
    assert "NEW 1" in next_day.summary
    (recorded,) = _events(tmp_path / "data", "ap.invoice.recorded")
    assert recorded["payload"]["file"] == "NAI_PO.pdf"

    # Recorded: the file drops out of the retry set, the day leaves the key,
    # and later day changes replay instead of re-dialing.
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-06")
    assert _run_intake(landing, tmp_path / "data").status == "noop"
    assert extractor.calls["NAI_PO.pdf"] == 3


def test_repeat_transport_failure_reflags_on_a_distinct_key_and_keeps_retrying(
    tmp_path, monkeypatch
):
    """Bad days in a row: every repeat failure lands as its own event (the
    first is not silently absorbed), the md5 still ends each key so the
    unprocessed pile and dismiss/identify keep finding the file, and the
    next day still retries, once."""
    landing = _make_landing(tmp_path)
    extractor = _FlakyExtractor(fail={"NAI_PO.pdf": (3, "timeout", True)})
    _use(monkeypatch, extractor)

    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-04")
    _run_intake(landing, tmp_path / "data")  # flag 1
    _run_intake(landing, tmp_path / "data")  # the one immediate retry: flag 2
    assert _run_intake(landing, tmp_path / "data").status == "noop"
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-05")
    second_day = _run_intake(landing, tmp_path / "data")  # flag 3

    assert second_day.status == "ok"
    assert "FLAGGED 1" in second_day.summary
    assert extractor.calls["NAI_PO.pdf"] == 3
    flags = _events(tmp_path / "data", "ap.intake.flagged")
    assert len(flags) == 3
    assert len({f["idempotency_key"] for f in flags}) == 3
    md5 = ap_jobs._md5(landing / "NAI_PO.pdf")
    assert all(f["idempotency_key"].endswith(f":{md5}") for f in flags)

    from core.agents.ap.unprocessed import md5_for_file, unresolved_unprocessed
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path / "data")) as ledger:
        assert md5_for_file(ledger, "NAI_PO.pdf") == md5
        assert [u["md5"] for u in unresolved_unprocessed(ledger, "demo")] == [md5]

    assert _run_intake(landing, tmp_path / "data").status == "noop"  # same day: replay
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-06")
    third_day = _run_intake(landing, tmp_path / "data")
    assert third_day.status == "ok"
    assert extractor.calls["NAI_PO.pdf"] == 4
    assert "NEW 1" in third_day.summary


def test_oversize_flag_never_retries_even_on_a_new_day(tmp_path, monkeypatch):
    """``oversize`` (like ``bad_reply``) is a verdict about the document: a
    new day changes nothing, so the run replays and the model is never
    dialed again for it."""
    landing = _make_landing(tmp_path)
    extractor = _FlakyExtractor(fail={"NAI_PO.pdf": (99, "oversize", False)})
    _use(monkeypatch, extractor)
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-04")

    first = _run_intake(landing, tmp_path / "data")
    assert "FLAGGED 1" in first.summary

    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-05")
    next_day = _run_intake(landing, tmp_path / "data")

    assert next_day.status == "noop"
    assert extractor.calls["NAI_PO.pdf"] == 1
    assert len(_events(tmp_path / "data", "ap.intake.flagged")) == 1


def test_bad_reply_flag_never_retries_even_on_a_new_day(tmp_path, monkeypatch):
    landing = _make_landing(tmp_path)
    extractor = _FlakyExtractor(fail={"NAI_PO.pdf": (99, "bad_reply", False)})
    _use(monkeypatch, extractor)
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-04")
    _run_intake(landing, tmp_path / "data")

    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-05")
    assert _run_intake(landing, tmp_path / "data").status == "noop"
    assert extractor.calls["NAI_PO.pdf"] == 1


def test_retry_day_enters_the_key_only_while_a_retryable_flag_names_a_candidate(
    tmp_path, monkeypatch
):
    """The key changes exactly when the retry day changes and a retryable
    flag exists: a clean folder ignores the day, a retryable flag makes it
    an input, and a flagged file that is no longer a candidate (archived,
    outside ``since``) drops it again so nothing churns daily for nothing."""
    from core.engine.config import load_tenant
    from core.engine.contracts import JobContext
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    landing = _make_landing(tmp_path)
    ledger_dir = tmp_path / "data"

    def key_on(day: str) -> str:
        monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: day)
        with Ledger.open(resolve_ledger_root("demo", ledger_dir)) as ledger:
            ctx = JobContext(
                tenant=load_tenant("demo"),
                tenant_slug="demo",
                ledger=ledger,
                agent="ap",
                job="intake",
                shadow=True,
                params={"landing_dir": str(landing), "extractor": "fixture"},
                agent_dir=Path(ap_jobs.__file__).parent,
            )
            return ap_jobs._intake_key(ctx)

    # Ensure the ledger exists before keying against it.
    _use(monkeypatch, _FlakyExtractor(fail={"NAI_PO.pdf": (99, "transport_error", True)}))
    monkeypatch.setattr(ap_jobs, "_retry_day", lambda ctx: "2026-09-04")

    # A clean, unflagged folder: the day is not an input.
    (landing / "NAI_PO.pdf").rename(landing / "hold.tmp")
    _run_intake(landing, ledger_dir)  # empty landing, no flags
    assert key_on("2026-09-04") == key_on("2026-09-05")
    (landing / "hold.tmp").rename(landing / "NAI_PO.pdf")

    # Flag it (transport): now the day is an input.
    _run_intake(landing, ledger_dir)
    assert key_on("2026-09-04") != key_on("2026-09-05")
    assert key_on("2026-09-05") == key_on("2026-09-05")

    # The flagged file leaves the candidate set (archived by the janitor, or
    # outside a ``since`` cutoff): the day leaves the key with it.
    (landing / "_archive").mkdir()
    (landing / "NAI_PO.pdf").rename(landing / "_archive" / "NAI_PO.pdf")
    assert key_on("2026-09-04") == key_on("2026-09-05")
