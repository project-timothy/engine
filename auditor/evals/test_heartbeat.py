"""Heartbeat lens evals: dead machinery must surface, live machinery must not."""

from __future__ import annotations

import os

from auditor.lenses import heartbeat
from auditor.store import AuditorStore

from .fixtures import NOW, add_run, commit, init_ledger_repo, make_context, make_ledger

FRESH = "2026-07-21T05:00:00+00:00"  # 1h before NOW
STALE = "2026-07-19T05:00:00+00:00"  # 49h before NOW


def _conditions(findings):
    return sorted(f.condition for f in findings)


# ---- daily runs ------------------------------------------------------------


def test_fresh_clean_runs_are_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="ap", job="intake", status="ok", created_at=FRESH)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake")])
    with ctx.ledger:
        assert heartbeat.check_daily_runs(ctx) == []


def test_job_that_never_ran_is_critical(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake")])
    with ctx.ledger:
        findings = heartbeat.check_daily_runs(ctx)
    assert _conditions(findings) == ["never-ran"]
    assert findings[0].severity == "CRITICAL"


def test_whole_engine_gone_stale_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="ap", job="intake", status="ok", created_at=STALE)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake")])
    with ctx.ledger:
        findings = heartbeat.check_engine_liveness(ctx)
    assert _conditions(findings) == ["stale"]
    assert findings[0].severity == "CRITICAL"


def test_replayed_quiet_job_is_not_dead(tmp_path):
    # Live false positive, 2026-07-20: timesheets intake had replayed for a
    # week (same files = same idempotency key = no fresh run row) while the
    # daily run fired every morning. A stale per-job timestamp next to a
    # fresh engine pulse is a healthy quiet stage, not a dead one.
    conn = make_ledger(tmp_path)
    add_run(conn, agent="timesheets", job="intake", status="ok", created_at=STALE)
    add_run(conn, agent="ap", job="intake", status="ok", created_at=FRESH)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake"), ("timesheets", "intake")])
    with ctx.ledger:
        findings = [*heartbeat.check_engine_liveness(ctx), *heartbeat.check_daily_runs(ctx)]
    assert findings == []


def test_errored_latest_run_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="ap", job="intake", status="error", created_at=FRESH)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake")])
    with ctx.ledger:
        assert _conditions(heartbeat.check_daily_runs(ctx)) == ["errored"]


def test_shadow_runs_do_not_prove_liveness(tmp_path):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="ap", job="intake", status="ok", created_at=STALE)
    add_run(conn, agent="ap", job="intake", status="ok", shadow=1, created_at=FRESH)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake")])
    with ctx.ledger:
        assert _conditions(heartbeat.check_engine_liveness(ctx)) == ["stale"]


def test_tenant_without_daily_jobs_expects_no_pulse(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path, expected_daily_jobs=[])
    with ctx.ledger:
        assert heartbeat.check_engine_liveness(ctx) == []


def test_recovered_run_supersedes_an_earlier_error(tmp_path):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="ap", job="intake", status="error", created_at=STALE)
    add_run(conn, agent="ap", job="intake", status="ok", created_at=FRESH)
    ctx = make_context(tmp_path, expected_daily_jobs=[("ap", "intake")])
    with ctx.ledger:
        assert heartbeat.check_daily_runs(ctx) == []


# ---- ledger backup ---------------------------------------------------------


def test_pushed_repo_is_quiet(tmp_path):
    make_ledger(tmp_path)
    init_ledger_repo(tmp_path)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert heartbeat.check_ledger_backup(ctx) == []


def test_not_a_repo_is_critical(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert _conditions(heartbeat.check_ledger_backup(ctx)) == ["not-a-repo"]


def test_never_pushed_is_critical(tmp_path):
    make_ledger(tmp_path)
    init_ledger_repo(tmp_path)
    import subprocess

    subprocess.run(
        ["git", "-C", str(tmp_path), "update-ref", "-d", "refs/remotes/origin/main"],
        check=True,
        capture_output=True,
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert _conditions(heartbeat.check_ledger_backup(ctx)) == ["no-remote-ref"]


def test_old_unpushed_commit_is_critical(tmp_path):
    make_ledger(tmp_path)
    init_ledger_repo(tmp_path)
    commit(tmp_path, date=STALE)  # unpushed, 49h old
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = heartbeat.check_ledger_backup(ctx)
    assert _conditions(findings) == ["unpushed-too-long"]
    assert findings[0].severity == "CRITICAL"


def test_young_unpushed_commit_is_normal_intraday_lag(tmp_path):
    make_ledger(tmp_path)
    init_ledger_repo(tmp_path)
    commit(tmp_path, date=FRESH)  # today's writes; tonight's push handles them
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert heartbeat.check_ledger_backup(ctx) == []


# ---- qbo token -------------------------------------------------------------


def _token_ctx(tmp_path, *, mtime_days_ago: float | None):
    token = tmp_path / "qbo-tokens.json"
    if mtime_days_ago is not None:
        token.write_text("{}")
        stamp = make_context(tmp_path).now.timestamp() - mtime_days_ago * 86400
        os.utime(token, (stamp, stamp))
    return make_context(tmp_path, qbo_token_file=str(token))


def test_fresh_token_is_quiet(tmp_path):
    make_ledger(tmp_path)
    ctx = _token_ctx(tmp_path, mtime_days_ago=1)
    with ctx.ledger:
        assert heartbeat.check_qbo_token(ctx) == []


def test_missing_token_is_critical(tmp_path):
    make_ledger(tmp_path)
    ctx = _token_ctx(tmp_path, mtime_days_ago=None)
    with ctx.ledger:
        assert _conditions(heartbeat.check_qbo_token(ctx)) == ["missing"]


def test_stale_token_warns_then_escalates(tmp_path):
    make_ledger(tmp_path)
    ctx = _token_ctx(tmp_path, mtime_days_ago=5)
    with ctx.ledger:
        warned = heartbeat.check_qbo_token(ctx)
    assert [f.severity for f in warned] == ["WARN"]
    ctx = _token_ctx(tmp_path, mtime_days_ago=45)
    with ctx.ledger:
        escalated = heartbeat.check_qbo_token(ctx)
    assert [f.severity for f in escalated] == ["CRITICAL"]


def test_unconfigured_token_is_out_of_scope(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path, qbo_token_file="")
    with ctx.ledger:
        assert heartbeat.check_qbo_token(ctx) == []


# ---- the auditor's own previous run ---------------------------------------


def test_first_night_has_no_previous_to_judge(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert heartbeat.check_previous_audit(ctx) == []


def test_previous_clean_run_is_quiet(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path)
    with AuditorStore.open(ctx.store_root) as store:
        run_id = store.start_run("t", now="2026-07-20T06:00:00+00:00")
        store.finish_run(run_id, status="ok", now="2026-07-20T06:00:30+00:00")
        store.start_run("t", now=NOW)  # tonight's own row must not count
    with ctx.ledger:
        assert heartbeat.check_previous_audit(ctx) == []


def test_previous_failed_run_is_critical(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path)
    with AuditorStore.open(ctx.store_root) as store:
        run_id = store.start_run("t", now="2026-07-20T06:00:00+00:00")
        store.finish_run(run_id, status="error", now="2026-07-20T06:00:30+00:00", error="boom")
    with ctx.ledger:
        findings = heartbeat.check_previous_audit(ctx)
    assert _conditions(findings) == ["did-not-finish-clean"]
    assert "boom" in findings[0].detail


def test_previous_crashed_mid_run_is_critical(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path)
    with AuditorStore.open(ctx.store_root) as store:
        store.start_run("t", now="2026-07-20T06:00:00+00:00")  # never finished
    with ctx.ledger:
        findings = heartbeat.check_previous_audit(ctx)
    assert _conditions(findings) == ["did-not-finish-clean"]
    assert "crashed" in findings[0].detail
