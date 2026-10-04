"""Timesheets intake: submitted hours are never lost, never unguarded.

2026-07-11 origin incident: the old-stack timesheet processor had been idle
since early June while the owner pulled hours by ad-hoc prompt, believing
automation handled it; and the AP janitor's junk rule was archiving fresh
timesheet CSVs the same day they arrived. This agent closes both gaps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.timesheets.schema import (
    TimesheetParseError,
    is_timesheet_name,
    parse_timesheet_csv,
)

CSV_TEXT = """﻿Consultant,Jordan Sample
Week Ending,2026-07-10
Total Hours,3.25
Submitted,2026-07-10T19:11:05.819Z

Project,Task/Description,Saturday,Sunday,Monday,Tuesday,Wednesday,Thursday,Friday,Total
P26_2029,Model,0,0,0,0,1.75,1.5,0,3.25
"DAILY TOTALS","",0.00,0.00,0.00,0.00,1.75,1.50,0.00,3.25
"""

CSV_NAME = "timesheet_jordan_sample_2026-07-10_20260710-8ESSN6.csv"
XLSX_NAME = "timesheet_jordan_sample_2026-07-10_20260710-8ESSN6.xlsx"


# ---------- parser contract --------------------------------------------------


def test_parser_reads_the_real_form_format():
    sub = parse_timesheet_csv(CSV_TEXT)
    assert sub.person == "Jordan Sample"
    assert sub.week_ending == "2026-07-10"
    assert sub.total_hours == 3.25
    assert sub.month == "2026-07"
    assert [(line.project, line.hours) for line in sub.lines] == [("P26_2029", 3.25)]


def test_parser_refuses_an_hours_mismatch():
    bad = CSV_TEXT.replace("Total Hours,3.25", "Total Hours,8.00")
    with pytest.raises(TimesheetParseError):
        parse_timesheet_csv(bad)


def test_filename_pattern_matches_the_form_and_nothing_else():
    assert is_timesheet_name(CSV_NAME) and is_timesheet_name(XLSX_NAME)
    assert not is_timesheet_name("Part Costs.xlsx")
    assert not is_timesheet_name("timesheet-notes.docx")


# ---------- intake job -------------------------------------------------------


def _make_landing(tmp_path: Path) -> Path:
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / CSV_NAME).write_text(CSV_TEXT, encoding="utf-8")
    (landing / XLSX_NAME).write_bytes(b"xlsx companion bytes")
    return landing


def _run_intake(landing: Path, filing: Path, ledger_dir: Path):
    from core.engine.runner import run

    return run(
        "demo",
        "timesheets",
        "intake",
        shadow=False,
        params={"landing_dir": str(landing), "filing_dir": str(filing)},
        ledger_dir=ledger_dir,
    )


def test_intake_records_files_and_queues_the_hours_card(tmp_path):
    landing = _make_landing(tmp_path)
    filing = tmp_path / "Timesheets"

    result = _run_intake(landing, filing, tmp_path / "data")

    assert result.status == "needs_approval"
    # filed copies, month folder from the week-ending date, originals intact
    assert (filing / "2026-07" / CSV_NAME).exists()
    assert (filing / "2026-07" / XLSX_NAME).exists()
    assert (landing / CSV_NAME).exists()
    # one approval card with the payroll-ready numbers
    (card,) = result.approvals_needed
    assert card.action_type == "timesheets.payroll_hours"
    assert card.params["person"] == "Jordan Sample"
    assert card.params["week_ending"] == "2026-07-10"
    assert card.params["total_hours"] == 3.25

    # idempotent: same folder state re-runs as a noop
    again = _run_intake(landing, filing, tmp_path / "data")
    assert again.status == "noop"


def test_malformed_submission_flags_and_stays(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / CSV_NAME).write_text(
        CSV_TEXT.replace("3.25\n", "not-a-number\n", 1), encoding="utf-8"
    )
    filing = tmp_path / "Timesheets"

    result = _run_intake(landing, filing, tmp_path / "data")

    assert result.status == "ok"
    assert any(a.code == "timesheets.parse_failed" for a in result.anomalies)
    assert (landing / CSV_NAME).exists()  # stays visible for a human
    assert not (filing / "2026-07").exists()


def test_ap_janitor_defers_to_timesheets_then_archives_settled(tmp_path):
    """The junk rule must never eat an unrecorded timesheet; once the
    timesheets agent records it, the janitor archives it like anything
    settled."""
    from core.engine.runner import run

    landing = _make_landing(tmp_path)
    filing = tmp_path / "Timesheets"
    ledger_dir = tmp_path / "data"

    run(
        "demo",
        "ap",
        "janitor",
        shadow=False,
        params={"landing_dir": str(landing), "days": "30"},
        ledger_dir=ledger_dir,
    )
    assert (landing / CSV_NAME).exists()  # fresh + unrecorded: untouched
    assert (landing / XLSX_NAME).exists()

    _run_intake(landing, filing, ledger_dir)
    run(
        "demo",
        "ap",
        "janitor",
        shadow=False,
        params={"landing_dir": str(landing), "days": "29"},
        ledger_dir=ledger_dir,
    )
    assert not (landing / CSV_NAME).exists()  # recorded: archived
    assert not (landing / XLSX_NAME).exists()
    assert (landing / "_archive").exists()


def test_cloud_only_companion_defers_the_pair_then_files_both(tmp_path, monkeypatch):
    """Honesty audit 2026-09-03, 03-F5 (S2). A cloud-only xlsx companion was
    skipped silently while the csv was recorded, so the pair counted as seen
    and the xlsx could never be filed. Now the pair waits together, with an
    anomaly, and files together once the placeholder materializes."""
    import errno

    from core.agents.timesheets import jobs as ts_jobs

    landing = _make_landing(tmp_path)
    filing = tmp_path / "Timesheets"
    xlsx = landing / XLSX_NAME
    real = Path.read_bytes

    def _evicted(self):
        if self == xlsx:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(self))
        return real(self)

    monkeypatch.setattr(ts_jobs.Path, "read_bytes", _evicted)
    first = _run_intake(landing, filing, tmp_path / "data")

    assert first.status == "ok"  # nothing recorded, no card: the pair waits
    assert any(a.code == "timesheets.cloud_only_deferred" for a in first.anomalies)
    assert first.approvals_needed == []
    assert not (filing / "2026-07" / CSV_NAME).exists()
    assert not (filing / "2026-07" / XLSX_NAME).exists()

    monkeypatch.setattr(ts_jobs.Path, "read_bytes", real)
    second = _run_intake(landing, filing, tmp_path / "data")

    assert second.status == "needs_approval"
    assert (filing / "2026-07" / CSV_NAME).exists()
    assert (filing / "2026-07" / XLSX_NAME).exists()
    (card,) = second.approvals_needed
    assert card.params["total_hours"] == 3.25


# ---------- shadow honesty (audit 2026-09-03, 03-F2) -------------------------


def _run_intake_shadow(landing: Path, filing: Path, ledger_dir: Path):
    from core.engine.runner import run

    return run(
        "demo",
        "timesheets",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "filing_dir": str(filing)},
        ledger_dir=ledger_dir,
    )


def _ledger_state(ledger_dir: Path):
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", ledger_dir)) as ledger:
        events = [
            e
            for e in ledger.read_event_log()
            if str(e.get("event_type", "")).startswith("timesheets.")
        ]
        cards = ledger.list_approvals("demo")
    return events, cards


def test_shadow_files_nothing_and_records_nothing(tmp_path):
    """Honesty audit 2026-09-03, 03-F2 (S2). A shadow run used to record
    `timesheets.recorded` with a `filed_to` path that was never written and
    park a real payroll card; the next real run then treated the pair as seen
    and never filed it, and the AP janitor archived the originals. In shadow
    the job says what it would do and leaves the ledger untouched."""
    landing = _make_landing(tmp_path)
    filing = tmp_path / "Timesheets"

    result = _run_intake_shadow(landing, filing, tmp_path / "data")

    assert result.status == "ok"
    assert result.approvals_needed == []
    assert not filing.exists()  # no month folder, no files
    assert (landing / CSV_NAME).exists() and (landing / XLSX_NAME).exists()
    events, cards = _ledger_state(tmp_path / "data")
    assert events == []
    assert cards == []
    # the actions name the work, in the would-do voice
    assert any(
        a.startswith("would file ") and CSV_NAME in a and "2026-07" in a for a in result.actions
    )
    assert any(a.startswith("would file ") and XLSX_NAME in a for a in result.actions)
    assert any(
        a.startswith("would park hours card") and "Jordan Sample" in a and "2026-07-10" in a
        for a in result.actions
    )
    assert "would record 1" in result.summary


def test_real_run_after_shadow_records_the_pair(tmp_path):
    """The real run right after a shadow run files both copies, records the
    pair, and parks the card, exactly as if the shadow run never happened."""
    landing = _make_landing(tmp_path)
    filing = tmp_path / "Timesheets"

    _run_intake_shadow(landing, filing, tmp_path / "data")
    result = _run_intake(landing, filing, tmp_path / "data")

    assert result.status == "needs_approval"
    assert (filing / "2026-07" / CSV_NAME).exists()
    assert (filing / "2026-07" / XLSX_NAME).exists()
    (card,) = result.approvals_needed
    assert card.action_type == "timesheets.payroll_hours"
    assert card.params["total_hours"] == 3.25
    events, cards = _ledger_state(tmp_path / "data")
    assert sorted(e["event_type"] for e in events) == [
        "timesheets.companion_filed",
        "timesheets.recorded",
    ]
    (recorded,) = [e for e in events if e["event_type"] == "timesheets.recorded"]
    assert Path(recorded["payload"]["filed_to"]).exists()
    assert [c["status"] for c in cards] == ["pending"]
