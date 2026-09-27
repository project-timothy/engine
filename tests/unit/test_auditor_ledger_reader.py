"""The auditor's read-only view of the engine ledger.

Independence principle 3 (docs/auditor-design.md): the auditor writes nowhere
the engine writes. For the ledger that is enforced at the CONNECTION, not by
discipline: the SQLite handle is opened ``mode=ro``, so a write attempt is an
error, not a hazard.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from auditor.ledger_reader import LedgerReader, resolve_ledger_root


def _seed_ledger(root):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "ledger.sqlite3")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, job TEXT)")
    conn.execute("INSERT INTO runs (job) VALUES ('intake')")
    conn.commit()
    conn.close()


def test_reads_with_its_own_sql(tmp_path):
    _seed_ledger(tmp_path)
    with LedgerReader.open(tmp_path) as reader:
        rows = reader.query("SELECT job FROM runs")
    assert [r["job"] for r in rows] == ["intake"]


def test_connection_is_readonly(tmp_path):
    _seed_ledger(tmp_path)
    with LedgerReader.open(tmp_path) as reader:
        with pytest.raises(sqlite3.OperationalError):
            reader.conn.execute("INSERT INTO runs (job) VALUES ('sneak')")


def test_missing_ledger_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        LedgerReader.open(tmp_path / "nowhere")


def test_events_parse_and_missing_log_is_empty(tmp_path):
    _seed_ledger(tmp_path)
    with LedgerReader.open(tmp_path) as reader:
        assert reader.events() == []
    (tmp_path / "event_log.jsonl").write_text(
        json.dumps({"event_type": "x", "payload": {"file": "a.pdf"}}) + "\n"
    )
    with LedgerReader.open(tmp_path) as reader:
        events = reader.events()
    assert events[0]["payload"]["file"] == "a.pdf"


def test_ledger_root_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGINE_LEDGER_ROOT", raising=False)
    assert resolve_ledger_root("t", str(tmp_path)) == tmp_path / "t"
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "env"))
    assert resolve_ledger_root("t", None) == tmp_path / "env" / "t"
