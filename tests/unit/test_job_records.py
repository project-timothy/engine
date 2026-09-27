"""Job-time durable records (honesty audit #170, finding 03-F9; owner go
2026-09-10).

Events persist only after a job returns, so a job that moves a file and then
dies leaves the move with no record. A job record is durable THE MOMENT the
job calls ``ctx.record_now``; the runner links it to the run row when the
run lands, ok or FAILED. These evals pin that contract at the substrate:
durable before return, idempotent on the key, linked on success, linked on
failure (and counted in the ``engine.run_failed`` event), nothing in shadow,
and readable by a later run through ``ctx.records``.
"""

from __future__ import annotations

import json

import pytest

from core.engine import runner as runner_mod
from core.engine.contracts import JobHandler, JobOutput
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

RECORD = "demo.move.recorded"


def _handler(fn, key: str = "k1"):
    return JobHandler(key=lambda ctx: key, run=fn)


def _records(root, record_type=RECORD):
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT idempotency_key, run_key, run_id, record_type, payload_json "
            "FROM job_records WHERE record_type = ? ORDER BY id",
            (record_type,),
        ).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in rows]


def _runs(root):
    with Ledger.open(root) as ledger:
        return [
            dict(r)
            for r in ledger.conn.execute("SELECT id, idempotency_key, status FROM runs").fetchall()
        ]


def test_record_now_is_durable_before_the_job_returns(tmp_path, monkeypatch):
    """The record is committed at the call, visible to a second connection
    while the job is still running (no atomic block wraps the job)."""
    seen: dict = {}

    def job(ctx):
        assert ctx.record_now("move:a", RECORD, {"file": "a.pdf", "dest": "x/a.pdf"}) is True
        assert ctx.record_now("move:a", RECORD, {"file": "a.pdf"}) is False  # idempotent
        root = resolve_ledger_root("demo", tmp_path)
        seen["mid_run"] = _records(root)
        return JobOutput(status="ok", summary="moved")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(job))
    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert result.status == "ok"
    (mid,) = seen["mid_run"]
    assert mid["run_id"] is None, "unlinked while the job runs: the run row does not exist yet"
    assert mid["payload"] == {"file": "a.pdf", "dest": "x/a.pdf"}, "first write wins"
    root = resolve_ledger_root("demo", tmp_path)
    (after,) = _records(root)
    (run_row,) = _runs(root)
    assert after["run_id"] == run_row["id"], "linked to the run that landed"
    assert after["run_key"] == run_row["idempotency_key"]
    assert after["idempotency_key"] == f"{run_row['idempotency_key']}:rec:move:a"


def test_a_dying_job_leaves_its_record_linked_to_the_failed_run(tmp_path, monkeypatch):
    """The whole point: record, move, die. The record survives and belongs to
    the FAILED run row, and the run_failed event counts it."""

    def job(ctx):
        ctx.record_now("move:b", RECORD, {"file": "b.pdf"})
        raise RuntimeError("died after the move")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(job))
    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert result.status == "error"
    root = resolve_ledger_root("demo", tmp_path)
    (rec,) = _records(root)
    (failed,) = _runs(root)
    assert failed["status"] == "error"
    assert rec["run_id"] == failed["id"]
    with Ledger.open(root) as ledger:
        (ev,) = [e for e in ledger.read_event_log() if e["event_type"] == "engine.run_failed"]
    assert ev["payload"]["job_records"] == 1


def test_a_later_run_reads_the_record_and_a_replay_writes_nothing_new(tmp_path, monkeypatch):
    def first(ctx):
        ctx.record_now("move:c", RECORD, {"file": "c.pdf"})
        raise RuntimeError("died")

    def second(ctx):
        found = ctx.records(RECORD)
        return JobOutput(status="ok", summary=f"saw {len(found)} record(s): {found[0]['payload']}")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(first, key="k1"))
    assert run("demo", "demo", "x", ledger_dir=tmp_path).status == "error"
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(second, key="k2"))
    healed = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert healed.status == "ok"
    assert "saw 1 record(s): {'file': 'c.pdf'}" in healed.summary
    # a replay of the healed run touches no records
    replay = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert replay.status == "noop" and healed.summary in replay.summary
    assert len(_records(resolve_ledger_root("demo", tmp_path))) == 1


def test_shadow_runs_record_nothing(tmp_path, monkeypatch):
    def job(ctx):
        assert ctx.record_now("move:d", RECORD, {"file": "d.pdf"}) is False
        return JobOutput(status="ok", summary="dry")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(job))
    assert run("demo", "demo", "x", ledger_dir=tmp_path, shadow=True).status == "ok"
    assert _records(resolve_ledger_root("demo", tmp_path)) == []


def test_record_outside_a_run_key_is_refused_quietly():
    """A context with no run key (a key builder, a unit test) records nothing
    rather than writing an unowned row."""
    from core.engine.contracts import JobContext

    ctx = JobContext.__new__(JobContext)
    ctx.shadow = False
    ctx.run_key = ""
    assert ctx.record_now("x", RECORD, {}) is False


@pytest.mark.parametrize("bad", ["", "   "])
def test_link_requires_a_run_key(tmp_path, bad):
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        assert ledger.link_job_records(run_key=bad, run_id=1) == 0
