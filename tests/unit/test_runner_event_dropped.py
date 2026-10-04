"""Honesty audit 2026-09-03, substrate finding F2: an intra-run duplicate
event key must never lose an event silently.

``Ledger.append_event`` is INSERT OR IGNORE and returns False when the key
is already there. The runner namespaces every event under the run key, so
two ``EventSpec``s in ONE JobOutput with the same local ``key`` collide:
before this, the second was dropped, the run's actions and summary still
counted it, and nothing on the record (events are all the auditor reads)
could see the loss.

Now the drop itself is recorded: an ``engine.event_dropped`` event naming
the event type and key, and an ``engine.event_dropped`` anomaly on the
stored result so the CLI and ``--json`` consumers see it. The run stays
ok: a duplicate key is a job bug to surface, not a reason to lose the rest
of the run.
"""

from __future__ import annotations

import json

from core.engine import runner as runner_mod
from core.engine.contracts import EventSpec, JobHandler, JobOutput
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

_TWICE = JobHandler(
    key=lambda ctx: "twice",
    run=lambda ctx: JobOutput(
        status="ok",
        summary="two events under one key",
        actions=["first", "second"],
        events=[
            EventSpec(key="x", event_type="demo.thing", payload={"n": 1}),
            EventSpec(key="x", event_type="demo.thing", payload={"n": 2}),
            EventSpec(key="y", event_type="demo.other", payload={"n": 3}),
        ],
    ),
)


def _events(ledger, event_type):
    rows = ledger.conn.execute(
        "SELECT payload_json FROM events WHERE event_type = ? ORDER BY id", (event_type,)
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def test_intra_run_duplicate_event_key_is_recorded_as_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _TWICE)
    result = run("demo", "demo", "twice", ledger_dir=tmp_path)

    # The run itself is fine; the loss is visible on the result ...
    assert result.status == "ok"
    dropped = [a for a in result.anomalies if a.code == "engine.event_dropped"]
    assert len(dropped) == 1
    assert "demo.thing" in dropped[0].detail
    assert "'x'" in dropped[0].detail

    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        # ... and on the record: one demo.thing landed (the first), and the
        # drop of the second is an event of its own.
        assert _events(ledger, "demo.thing") == [{"n": 1}]
        assert _events(ledger, "demo.other") == [{"n": 3}]
        assert _events(ledger, "engine.event_dropped") == [{"event_type": "demo.thing", "key": "x"}]
        # The stored result carries the anomaly too (what a replay would show).
        row = ledger.conn.execute("SELECT status, result_json FROM runs").fetchone()
    assert row["status"] == "ok"
    stored = json.loads(row["result_json"])
    assert [a["code"] for a in stored["anomalies"]] == ["engine.event_dropped"]


def test_distinct_event_keys_record_no_drop(tmp_path):
    result = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert result.status == "ok"
    assert not [a for a in result.anomalies if a.code == "engine.event_dropped"]
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        assert _events(ledger, "engine.event_dropped") == []
