"""Cross-run retries (phase 7 row 7.23, issue #232).

A job may declare a bounded ``RetryPolicy`` on its ``JobHandler``. When such
a job fails with a cause the policy lists, the runner records the failure
exactly as before (the #172 trace: a FAILED run row under a key that never
replays, the ``engine.run_failed`` event, the FAILED commit) AND writes one
``engine.retry`` job record naming the attempt, the policy, and when the
next attempt is due. ``resume_due`` (the ``engine jobs resume`` command)
executes the due attempts one at a time under the run lock; each attempt
is the SAME run resumed under the ORIGINAL run key, so a retry that
succeeds after a partial write does not double-write.

This is the cross-run retry. The in-process ones (``RetryingExtractor``, the
gateway's tier fallback, the one validation retry) redial inside a single
run and are a different mechanism (docs/retries.md).

The fixture job is ``demo/flaky``: it fails ``fail_times`` times with the
named ``cause`` and succeeds after, recording one durable move on its first
attempt so the double-write question has a witness.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from core.agents.demo.jobs import JOBS as DEMO_JOBS
from core.engine.contracts import JobHandler, JobOutput, RetryPolicy
from core.engine.registry import UnknownJobError
from core.engine.runner import RETRY_RECORD, resolve_ledger_root, resume_due, run
from core.ledger import Ledger


def _flaky(tmp_path, **params):
    return run(
        "demo",
        "demo",
        "flaky",
        params={"fail_times": "2", "cause": "transport_error", **params},
        ledger_dir=tmp_path,
    )


def _runs(root):
    with Ledger.open(root) as ledger:
        return [
            dict(r)
            for r in ledger.conn.execute(
                "SELECT id, idempotency_key, status FROM runs ORDER BY id"
            ).fetchall()
        ]


def _retries(root):
    with Ledger.open(root) as ledger:
        return ledger.job_records(tenant="demo", record_type=RETRY_RECORD)


def _records(root, record_type):
    with Ledger.open(root) as ledger:
        return ledger.job_records(tenant="demo", record_type=record_type)


def _codes(result):
    return [a.code for a in result.anomalies]


def _due_at(record) -> datetime:
    return datetime.fromisoformat(record["next_attempt_at"])


# ---- the policy primitive --------------------------------------------------


def test_existing_handlers_declare_no_policy():
    """Default none: every job in the repo behaves exactly as today."""
    assert JobHandler(key=lambda c: "k", run=lambda c: JobOutput()).retry is None
    assert DEMO_JOBS["ingest"].retry is None
    from core.engine.registry import list_agents, load_agent_jobs

    holders = [
        f"{agent}/{name}"
        for agent in list_agents()
        for name, handler in load_agent_jobs(agent).items()
        if handler.retry is not None
    ]
    assert holders == ["demo/flaky"], "the fixture job is the only policy holder in this row"


def test_policy_shape_and_backoff_ladder():
    policy = RetryPolicy(max_attempts=3, backoff_seconds=[60, 300], retry_on=["transport_error"])
    assert policy.delay_before(2) == 60
    assert policy.delay_before(3) == 300
    assert policy.delay_before(9) == 300, "the last rung repeats"
    assert policy.retries("transport_error") is True
    assert policy.retries("bad_reply") is False
    assert json.loads(policy.model_dump_json()) == {
        "max_attempts": 3,
        "backoff_seconds": [60, 300],
        "retry_on": ["transport_error"],
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"max_attempts": 1},
        {"max_attempts": 0},
        {"backoff_seconds": []},
        {"backoff_seconds": [-1]},
        {"retry_on": []},
    ],
)
def test_policy_rejects_a_budget_that_cannot_retry(bad):
    base = {"max_attempts": 3, "backoff_seconds": [60], "retry_on": ["transport_error"]}
    with pytest.raises(ValueError):
        RetryPolicy(**{**base, **bad})


# ---- the no-policy path: byte for byte what it was ------------------------

# The guarantee the 08:00 run, the auditor, and the Thursday sweep rest on:
# no job in production declares a policy, so nothing about a failure may
# change. Migration 8's columns sit unused, the 2026-09-03 trace is exactly
# what it was, and there is nothing for a resume to find.


def _no_policy_raises(ctx):
    raise RuntimeError("boom, no policy")


def _no_policy_reports_error(ctx):
    return JobOutput(status="error", summary="job says no")


def _no_policy_handler(fn) -> JobHandler:
    """A handler as every production handler is written: no ``retry``."""
    handler = JobHandler(key=lambda ctx: "k1", run=fn)
    assert handler.retry is None
    return handler


TRACE_PAYLOAD_KEYS = {"job", "run_key", "exception", "rows_changed", "job_records", "shadow"}


@pytest.mark.parametrize(
    "fn, expected_codes, expected_exception",
    [
        (_no_policy_raises, ["job.exception", "engine.run_failed"], "boom, no policy"),
        (_no_policy_reports_error, ["engine.run_failed"], "job says no"),
    ],
    ids=["raised", "reported"],
)
def test_a_job_with_no_policy_fails_exactly_as_it_did_before(
    tmp_path, monkeypatch, fn, expected_codes, expected_exception
):
    from core.engine import runner as runner_mod

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _no_policy_handler(fn))
    result = run("demo", "demo", "x", ledger_dir=tmp_path)

    # the result: the same anomalies in the same order, no retry anywhere in it
    assert result.status == "error"
    assert [a.code for a in result.anomalies] == expected_codes
    assert "retry" not in result.model_dump_json()
    assert result.commit, "the FAILED commit still owns the rows"

    root = resolve_ledger_root("demo", tmp_path)
    # the run row: one, error, under the never-replayable failed key
    (row,) = _runs(root)
    assert row["status"] == "error"
    assert row["idempotency_key"].startswith(result.idempotency_key + ":failed:")

    # the event: the same payload keys it carried before this row, no cause
    with Ledger.open(root) as ledger:
        events = [
            json.loads(r["payload_json"])
            for r in ledger.conn.execute(
                "SELECT payload_json FROM events WHERE event_type = 'engine.run_failed'"
            ).fetchall()
        ]
    (payload,) = events
    assert set(payload) == TRACE_PAYLOAD_KEYS
    assert expected_exception in payload["exception"]

    # nothing on the retry side: no record of any kind, nothing to resume
    with Ledger.open(root) as ledger:
        assert ledger.job_records(tenant="demo") == []
    assert _retries(root) == []
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []

    # and the original key is still free: the re-run executes as it always did
    second = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert second.status == "error"
    assert len(_runs(root)) == 2


# ---- the chain: fails twice, succeeds the third time -----------------------


def test_fails_twice_then_succeeds_leaves_three_linked_records_and_one_ok_run(tmp_path):
    first = _flaky(tmp_path)
    assert first.status == "error"
    assert "engine.run_failed" in _codes(first), "the #172 trace is not weakened"
    assert "engine.retry.scheduled" in _codes(first)
    root = resolve_ledger_root("demo", tmp_path)

    second = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert [r.status for r in second] == ["error"]
    third = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert [r.status for r in third] == ["ok"]
    assert third[0].idempotency_key == first.idempotency_key, "the retry is the same run"
    assert "engine.retry.attempt" in _codes(third[0])

    # ONE final run row marked ok under the original key; the two FAILED
    # traces preserved under their never-replayable keys.
    rows = _runs(root)
    assert [r["status"] for r in rows] == ["error", "error", "ok"]
    assert rows[2]["idempotency_key"] == first.idempotency_key
    assert all(
        r["idempotency_key"].startswith(first.idempotency_key + ":failed:") for r in rows[:2]
    )

    # three linked records: attempt 1 -> 2 -> 3, each owning its run row
    r1, r2, r3 = _retries(root)
    assert [r["attempt"] for r in (r1, r2, r3)] == [1, 2, 3]
    assert r1["parent_record_id"] is None
    assert r2["parent_record_id"] == r1["id"]
    assert r3["parent_record_id"] == r2["id"]
    assert [r["run_id"] for r in (r1, r2, r3)] == [rows[0]["id"], rows[1]["id"], rows[2]["id"]]
    assert [r["retry_state"] for r in (r1, r2, r3)] == ["resumed", "resumed", "succeeded"]
    assert r3["next_attempt_at"] is None
    assert all(r["run_key"] == first.idempotency_key for r in (r1, r2, r3))
    assert json.loads(r1["retry_policy"])["max_attempts"] == 3
    assert r1["payload"]["cause"] == "transport_error"
    assert r1["payload"]["params"] == {"fail_times": "2", "cause": "transport_error"}

    # nothing left to resume, and the ok run replays as a no-op
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []
    assert _flaky(tmp_path).status == "noop"


def test_a_retry_after_a_partial_write_does_not_double_write(tmp_path):
    """The first attempt recorded its move (record-then-move) before dying.
    The attempt that succeeds finds the record under the SAME run key and
    skips the move: one record, owned by the FAILED run that made it."""
    first = _flaky(tmp_path)
    assert first.status == "error"
    root = resolve_ledger_root("demo", tmp_path)
    (moved,) = _records(root, "demo.flaky.moved")
    failed_1 = _runs(root)[0]
    assert moved["run_id"] == failed_1["id"]

    resume_due("demo", force=True, ledger_dir=tmp_path)
    (ok,) = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert ok.status == "ok"
    assert "already on the record" in ok.summary
    (still_one,) = _records(root, "demo.flaky.moved")
    assert still_one["id"] == moved["id"]
    assert still_one["run_id"] == failed_1["id"], "the trace keeps its owner"


# ---- the budget ------------------------------------------------------------


def test_past_the_budget_stays_failed_with_the_count_and_is_never_picked_again(tmp_path):
    first = _flaky(tmp_path, fail_times="5")
    assert first.status == "error"
    resume_due("demo", force=True, ledger_dir=tmp_path)
    (last,) = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert last.status == "error"
    (exhausted,) = [a for a in last.anomalies if a.code == "engine.retry.exhausted"]
    assert "attempt 3 of 3" in exhausted.detail
    root = resolve_ledger_root("demo", tmp_path)
    assert [r["status"] for r in _runs(root)] == ["error", "error", "error"]
    r1, r2, r3 = _retries(root)
    assert (r3["attempt"], r3["retry_state"], r3["next_attempt_at"]) == (3, "exhausted", None)
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []


def test_only_causes_in_retry_on_retry(tmp_path):
    """A validation verdict is not a blip: no retry record, nothing to resume."""
    result = _flaky(tmp_path, cause="bad_reply")
    assert result.status == "error"
    assert "engine.run_failed" in _codes(result)
    (why,) = [a for a in result.anomalies if a.code == "engine.retry.not_retryable"]
    assert "bad_reply" in why.detail
    root = resolve_ledger_root("demo", tmp_path)
    assert _retries(root) == []
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []


def test_a_failure_that_declares_itself_not_transient_is_not_retried(tmp_path):
    """Same cause label, but the exception says a redial cannot help (a 4xx
    from a provider is ``transport_error`` with ``transient=False``)."""
    result = _flaky(tmp_path, cause="transport_error", transient="no")
    assert result.status == "error"
    assert "engine.retry.not_retryable" in _codes(result)
    assert _retries(resolve_ledger_root("demo", tmp_path)) == []


def test_a_shadow_run_never_schedules_a_retry(tmp_path):
    result = run(
        "demo",
        "demo",
        "flaky",
        shadow=True,
        params={"fail_times": "2", "cause": "transport_error"},
        ledger_dir=tmp_path,
    )
    assert result.status == "error"
    assert "engine.retry.not_retryable" in _codes(result)
    assert _retries(resolve_ledger_root("demo", tmp_path)) == []


# ---- backoff and the clock -------------------------------------------------


def test_backoff_is_honored_and_the_clock_is_injectable(tmp_path):
    first = _flaky(tmp_path)
    assert first.status == "error"
    root = resolve_ledger_root("demo", tmp_path)
    (r1,) = _retries(root)
    due = _due_at(r1)
    scheduled_at = datetime.fromisoformat(r1["created_at"])
    assert due - scheduled_at == timedelta(seconds=60), "first rung of the demo ladder"

    assert resume_due("demo", as_of=due - timedelta(seconds=1), ledger_dir=tmp_path) == []
    assert _retries(root)[0]["retry_state"] == "scheduled", "not due: not touched"
    (second,) = resume_due("demo", as_of=due, ledger_dir=tmp_path)
    assert second.status == "error"
    r1, r2 = _retries(root)
    assert r1["retry_state"] == "resumed"
    assert _due_at(r2) - datetime.fromisoformat(r2["created_at"]) == timedelta(seconds=300)
    # --now (force) runs a retry that is not yet due: the owner's tool
    (third,) = resume_due("demo", as_of=due, force=True, ledger_dir=tmp_path)
    assert third.status == "ok"


def test_resume_with_nothing_due_is_a_quiet_no_op(tmp_path):
    assert resume_due("demo", ledger_dir=tmp_path) == []
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []


def test_resume_runs_one_at_a_time_under_the_run_lock(tmp_path, monkeypatch):
    """Each resumed attempt takes the ledger's write lock like any run; a
    held lock is a structured refusal and the record stays scheduled."""
    from core.engine import runner as runner_mod

    first = _flaky(tmp_path)
    assert first.status == "error"
    root = resolve_ledger_root("demo", tmp_path)
    with runner_mod.ledger_write_lock(root):
        (refused,) = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert refused.status == "error"
    assert "engine.run_locked" in _codes(refused)
    assert _retries(root)[0]["retry_state"] == "scheduled"
    (ok_later,) = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert ok_later.status == "error"  # attempt 2 of the fixture still fails


# ---- a hand re-run joins the chain -----------------------------------------


def test_a_hand_rerun_while_a_retry_is_pending_is_the_next_attempt(tmp_path):
    """The owner re-running the job by hand does not fork a second chain
    under the same key: it consumes the pending attempt."""
    first = _flaky(tmp_path)
    assert first.status == "error"
    second = _flaky(tmp_path)
    assert second.status == "error"
    assert "engine.retry.attempt" in _codes(second)
    root = resolve_ledger_root("demo", tmp_path)
    r1, r2 = _retries(root)
    assert (r1["retry_state"], r2["retry_state"], r2["attempt"]) == ("resumed", "scheduled", 2)
    assert r2["parent_record_id"] == r1["id"]
    (third,) = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert third.status == "ok"
    assert [r["status"] for r in _runs(root)] == ["error", "error", "ok"]


def test_a_retry_whose_job_no_longer_exists_is_exhausted_not_looped(tmp_path, monkeypatch):
    first = _flaky(tmp_path)
    assert first.status == "error"
    from core.engine import runner as runner_mod

    def _gone(agent, job):
        raise UnknownJobError(f"agent {agent!r} has no job {job!r}")

    monkeypatch.setattr(runner_mod, "get_job", _gone)
    (result,) = resume_due("demo", force=True, ledger_dir=tmp_path)
    assert result.status == "error"
    assert "engine.retry.exhausted" in _codes(result)
    root = resolve_ledger_root("demo", tmp_path)
    (r1,) = _retries(root)
    assert r1["retry_state"] == "exhausted"
    monkeypatch.undo()
    assert resume_due("demo", force=True, ledger_dir=tmp_path) == []
