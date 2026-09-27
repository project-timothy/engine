"""Owner decision 2026-09-03 (honesty audit, issue #170, the design gate):
a run that executed the job and failed leaves a trace.

Before: a raised or reported-error run recorded nothing and committed nothing
(a 2026-06-10 decision). Domain stores commit as they go, so the rows a job
changed before it failed stayed in the ledger with no run row, no event, and
no commit of their own; the next unrelated run's ``git add -A`` swept them
into a mis-attributed commit, and the auditor's heartbeat lens waited for an
error run row the engine never wrote.

Now: the failure is recorded under a key that can never be replayed
(``<run key>:failed:<stamp>``), with an ``engine.run_failed`` event naming
the job, the exception, and how many ledger rows changed before the
failure, and a ledger commit whose message says FAILED. The original key
stays free, so a re-run after the fix executes the job exactly as before.

The boundary: a run refused BEFORE the job ran (run lock, key computation
failure, strict key audit) still records nothing, because nothing could have
changed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from core.agents.ap import store
from core.engine import runner as runner_mod
from core.engine.contracts import JobHandler, JobOutput
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger


def _insert_row(ctx, number: str) -> None:
    store.insert_invoice(
        ctx.ledger,
        tenant=ctx.tenant_slug,
        vendor="Acme",
        invoice_number=number,
        amount_cents=100,
        status="Received",
        gl_account="COGS",
        invoice_date="2026-09-01",
    )


def _writes_then_raises(ctx):
    _insert_row(ctx, "A-1")
    _insert_row(ctx, "A-2")
    raise RuntimeError("boom after two rows")


def _writes_then_reports_error(ctx):
    _insert_row(ctx, "B-1")
    return JobOutput(status="error", summary="job says no")


def _flag_gated(ctx):
    flag = ctx.params.get("flag")
    if flag and __import__("pathlib").Path(flag).exists():
        raise RuntimeError("flag is up")
    return JobOutput(status="ok", summary="fine")


def _handler(fn):
    # The key declares the one param a handler here may read (strict key audit).
    return JobHandler(key=lambda ctx: f"k1:{ctx.params.get('flag', '')}", run=fn)


def _runs(root):
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT idempotency_key, status, summary FROM runs ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT payload_json FROM events WHERE event_type = ? ORDER BY id", (event_type,)
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]


def _last_commit_subject(root) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_raised_job_records_a_failed_run_an_event_and_a_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _handler(_writes_then_raises))

    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert result.status == "error"
    assert result.anomalies[0].code == "job.exception"  # the original error leads
    (trace,) = [a for a in result.anomalies if a.code == "engine.run_failed"]
    assert "ledger row change(s) before the failure" in trace.detail
    root = resolve_ledger_root("demo", tmp_path)
    (row,) = _runs(root)
    assert row["status"] == "error"
    assert row["idempotency_key"].startswith(result.idempotency_key + ":failed:")
    (ev,) = _events(root, "engine.run_failed")
    assert ev["job"] == "demo/x"
    assert ev["run_key"] == result.idempotency_key
    assert ev["rows_changed"] >= 2  # two inserts (the store counts every row-level change)
    assert "RuntimeError: boom after two rows" in ev["exception"]
    assert result.commit  # the partial writes have an owner commit
    assert "FAILED" in _last_commit_subject(root)


def test_reported_error_output_is_recorded_the_same_way(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_mod, "get_job", lambda agent, job: _handler(_writes_then_reports_error)
    )

    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert result.status == "error"
    root = resolve_ledger_root("demo", tmp_path)
    (row,) = _runs(root)
    assert row["status"] == "error"
    assert row["summary"] == "job says no"
    (ev,) = _events(root, "engine.run_failed")
    assert ev["rows_changed"] >= 1
    assert "job says no" in ev["exception"]


def test_failed_run_never_blocks_the_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _handler(_flag_gated))
    flag = tmp_path / "flag"
    flag.write_text("up")

    failed = run("demo", "demo", "x", params={"flag": str(flag)}, ledger_dir=tmp_path)
    assert failed.status == "error"

    flag.unlink()
    fixed = run("demo", "demo", "x", params={"flag": str(flag)}, ledger_dir=tmp_path)

    assert fixed.status == "ok"  # executed, not a replay of the failure
    root = resolve_ledger_root("demo", tmp_path)
    assert [r["status"] for r in _runs(root)] == ["error", "ok"]
    with Ledger.open(root) as ledger:
        assert ledger.find_run(fixed.idempotency_key).status == "ok"

    again = run("demo", "demo", "x", params={"flag": str(flag)}, ledger_dir=tmp_path)
    assert again.status == "noop"  # the success has idempotent identity, the failure never did


def test_two_failures_in_a_row_both_leave_a_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _handler(_writes_then_raises))

    first = run("demo", "demo", "x", ledger_dir=tmp_path)
    second = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert first.status == second.status == "error"
    root = resolve_ledger_root("demo", tmp_path)
    assert len(_runs(root)) == 2
    assert len(_events(root, "engine.run_failed")) == 2


def test_failure_recording_failure_never_masks_the_original_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _handler(_writes_then_raises))

    def _commit_blows_up(self, **kwargs):
        raise OSError("git is gone")

    monkeypatch.setattr(Ledger, "commit", _commit_blows_up)

    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert result.status == "error"
    assert result.anomalies[0].code == "job.exception"
    assert "boom after two rows" in result.anomalies[0].detail
    assert any(a.code == "engine.failure_unrecorded" for a in result.anomalies)
    assert result.commit is None


def test_refused_before_the_job_ran_records_nothing(tmp_path, monkeypatch):
    def _bad_key(ctx):
        raise ValueError("cannot compute key")

    monkeypatch.setattr(
        runner_mod, "get_job", lambda agent, job: JobHandler(key=_bad_key, run=_flag_gated)
    )

    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    assert result.status == "error"
    assert _runs(resolve_ledger_root("demo", tmp_path)) == []


def test_heartbeat_lens_sees_the_engine_written_failure(tmp_path, monkeypatch):
    """The auditor's 'errored' branch was dead against the live engine
    (honesty audit 04-F1): its eval inserted a status='error' row the engine
    never wrote. Now the engine writes one, and the lens reads it."""
    pytest.importorskip("auditor")
    from auditor.evals.fixtures import make_context
    from auditor.lenses import heartbeat

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _handler(_writes_then_raises))
    run("demo", "demo", "x", ledger_dir=tmp_path)

    root = resolve_ledger_root("demo", tmp_path)
    ctx = make_context(root, expected_daily_jobs=[("demo", "x")], slug="demo")
    with ctx.ledger:
        conditions = [f.condition for f in heartbeat.check_daily_runs(ctx)]
    assert conditions == ["errored"]
