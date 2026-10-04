"""Timesheets lens evals: submissions become hours in front of the owner."""

from __future__ import annotations

import os
from datetime import datetime

from auditor.lenses import timesheets

from .fixtures import NOW, add_approval, add_event, make_context, make_ledger

TS = "timesheet_chris_2026-07-10.csv"


def _file(directory, name, *, age_days: float = 3):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"csv")
    stamp = datetime.fromisoformat(NOW).timestamp() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _ctx(tmp_path, **overrides):
    return make_context(tmp_path / "ledger", **overrides)


def _record(conn, name=TS):
    add_event(conn, event_type="timesheets.recorded", payload={"file": name, "filed_to": "x"})


def _card(conn, name=TS):
    add_approval(
        conn,
        action_type="timesheets.payroll_hours",
        params={"file": name, "person": "C", "total_hours": 3.25},
    )


def test_recorded_with_card_is_quiet(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    _record(conn)
    _card(conn)
    ctx = _ctx(tmp_path)
    with ctx.ledger:
        assert timesheets.check(ctx) == []


def test_recorded_without_card_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    _record(conn)
    ctx = _ctx(tmp_path)
    with ctx.ledger:
        findings = timesheets.check(ctx)
    assert [f.condition for f in findings] == ["hours-never-queued"]


def test_filed_submission_with_record_is_quiet(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    _record(conn)
    _card(conn)
    _file(tmp_path / "filed" / "2026-07", TS)
    ctx = _ctx(tmp_path, timesheets_filing_dir=str(tmp_path / "filed"))
    with ctx.ledger:
        assert timesheets.check(ctx) == []


def test_filed_submission_without_record_is_a_finding(tmp_path):
    make_ledger(tmp_path / "ledger")
    _file(tmp_path / "filed" / "2026-07", TS)
    ctx = _ctx(tmp_path, timesheets_filing_dir=str(tmp_path / "filed"))
    with ctx.ledger:
        findings = timesheets.check(ctx)
    assert [f.condition for f in findings] == ["filed-without-record"]


def test_non_timesheet_files_in_the_tree_are_ignored(tmp_path):
    make_ledger(tmp_path / "ledger")
    _file(tmp_path / "filed", "Payscale Rates.xlsx")
    ctx = _ctx(tmp_path, timesheets_filing_dir=str(tmp_path / "filed"))
    with ctx.ledger:
        assert timesheets.check(ctx) == []


def test_stuck_unrecorded_submission_at_landing_is_a_finding(tmp_path):
    make_ledger(tmp_path / "ledger")
    _file(tmp_path / "landing", TS, age_days=5)
    ctx = _ctx(tmp_path, landing_dir=str(tmp_path / "landing"))
    with ctx.ledger:
        findings = timesheets.check(ctx)
    assert [f.condition for f in findings] == ["stuck-unrecorded"]


def test_fresh_landing_submission_gets_the_grace_window(tmp_path):
    make_ledger(tmp_path / "ledger")
    _file(tmp_path / "landing", TS, age_days=0.5)
    ctx = _ctx(tmp_path, landing_dir=str(tmp_path / "landing"))
    with ctx.ledger:
        assert timesheets.check(ctx) == []


def test_recorded_landing_copy_is_quiet(tmp_path):
    # The original stays in the landing folder as the audit artifact; a
    # recorded event covers it.
    conn = make_ledger(tmp_path / "ledger")
    _record(conn)
    _card(conn)
    _file(tmp_path / "landing", TS, age_days=5)
    ctx = _ctx(tmp_path, landing_dir=str(tmp_path / "landing"))
    with ctx.ledger:
        assert timesheets.check(ctx) == []


def test_legacy_files_are_out_of_scope(tmp_path):
    make_ledger(tmp_path / "ledger")
    _file(tmp_path / "filed" / "2026-05", TS, age_days=400)
    ctx = _ctx(
        tmp_path,
        timesheets_filing_dir=str(tmp_path / "filed"),
        coverage_since="2026-07-09",
    )
    with ctx.ledger:
        assert timesheets.check(ctx) == []


def test_configured_filing_dir_that_is_missing_warns(tmp_path):
    """Honesty audit 2026-09-03 (04-F4): a configured Timesheets tree that is
    absent was skipped silently; the lens went green over a folder it never read."""
    make_ledger(tmp_path / "ledger")
    ctx = _ctx(tmp_path, timesheets_filing_dir=str(tmp_path / "gone-filed"))
    with ctx.ledger:
        findings = timesheets.check(ctx)
    assert [f.condition for f in findings] == ["tree-missing"]
    assert findings[0].severity == "WARN"
    assert "gone-filed" in findings[0].detail


def test_configured_landing_dir_that_is_missing_warns(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = _ctx(tmp_path, landing_dir=str(tmp_path / "gone-landing"))
    with ctx.ledger:
        findings = timesheets.check(ctx)
    assert [f.condition for f in findings] == ["tree-missing"]
    assert "gone-landing" in findings[0].detail
