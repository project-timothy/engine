"""deadlines: dated obligations surfaced weeks early, each reminder once.

The Reminder construct (docs/boundary-rules.md): a dated obligation the engine
cannot execute itself (renew a passport, file a return, renew a registration),
surfaced ahead of time with its evidence. The promises under test:

1. Due dates are code. Recurrence rolls from the anchor without drift (a
   month-end anchor clamps, never creeps).
2. Each reminder fires once per lead window, at the tightest window crossed,
   so an obligation added five days out says "7 days" once, never 90, 30 and 7
   in one morning. Overdue fires once.
3. `done` closes an occurrence; a recurring obligation rolls to the next.
4. The calendar file carries every open occurrence with an alarm per lead
   window, so any calendar app shows it weeks early.
5. A malformed file is an error that names the problem, never a silent skip.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.agents.deadlines.schema import (
    ObligationsError,
    load_obligations,
    occurrence_dates,
)
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

OBLIGATIONS = """
[[obligation]]
id = "passport-jane"
title = "Passport renewal"
who = "Jane Doe"
kind = "passport"
due = 2027-03-01
notes = "Renew at the embassy; two photos."

[[obligation]]
id = "annual-report"
title = "Quarterly support report"
who = "Field unit 12"
kind = "report"
due = 2026-12-31
every = "3m"
lead_days = [14, 3]

[[obligation]]
id = "visa-kenya"
title = "Kenya work permit"
who = "Jane Doe"
kind = "visa"
due = 2026-10-09
"""


def _files(tmp_path: Path, text: str = OBLIGATIONS) -> dict:
    ob = tmp_path / "obligations.toml"
    ob.write_text(text)
    return {"obligations_file": str(ob), "ics_path": str(tmp_path / "out" / "deadlines.ics")}


def _scan(tmp_path: Path, today: str, *, text: str = OBLIGATIONS, shadow: bool = False):
    params = {**_files(tmp_path, text), "today": today}
    return run(
        "demo", "deadlines", "scan", params=params, ledger_dir=tmp_path / "data", shadow=shadow
    )


def _done(tmp_path: Path, ob_id: str, today: str, **extra):
    params = {**_files(tmp_path), "id": ob_id, "today": today, **extra}
    return run("demo", "deadlines", "done", params=params, ledger_dir=tmp_path / "data")


def _reminders(tmp_path: Path) -> list[dict]:
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        return [
            e["payload"] for e in ledger.read_event_log() if e["event_type"] == "deadlines.reminder"
        ]


# ---- 1. due dates are code ------------------------------------------------


def test_month_end_anchor_clamps_and_never_creeps():
    got = occurrence_dates(date(2026, 1, 31), "1m", until=date(2026, 6, 30))
    assert got == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
    ]


def test_every_unit_rolls_from_the_anchor():
    assert occurrence_dates(date(2026, 1, 1), "2w", until=date(2026, 1, 31)) == [
        date(2026, 1, 1),
        date(2026, 1, 15),
        date(2026, 1, 29),
    ]
    assert occurrence_dates(date(2024, 2, 29), "1y", until=date(2025, 3, 1)) == [
        date(2024, 2, 29),
        date(2025, 2, 28),
    ]
    assert occurrence_dates(date(2026, 5, 1), None, until=date(2030, 1, 1)) == [date(2026, 5, 1)]


# ---- 2. once per window, tightest window crossed --------------------------


def test_a_late_addition_says_the_tightest_window_once(tmp_path):
    result = _scan(tmp_path, "2026-10-04")  # the permit is 5 days out
    assert result.status == "ok"
    permit = [r for r in _reminders(tmp_path) if r["id"] == "visa-kenya"]
    assert [(r["tier"], r["days_left"]) for r in permit] == [("7", 5)]


def test_the_next_morning_repeats_nothing(tmp_path):
    _scan(tmp_path, "2026-10-04")
    _scan(tmp_path, "2026-10-05")
    permit = [r for r in _reminders(tmp_path) if r["id"] == "visa-kenya"]
    assert len(permit) == 1


def test_each_window_fires_as_it_is_crossed_and_then_overdue(tmp_path):
    for today in ("2026-11-30", "2026-12-01", "2026-12-17", "2026-12-28", "2027-01-01"):
        _scan(tmp_path, today)
    report = [r for r in _reminders(tmp_path) if r["id"] == "annual-report"]
    assert [(r["due"], r["tier"]) for r in report] == [
        ("2026-12-31", "14"),
        ("2026-12-31", "3"),
        ("2026-12-31", "overdue"),
    ]


def test_default_lead_days_are_ninety_thirty_seven(tmp_path):
    _scan(tmp_path, "2026-12-01")  # passport is 90 days out
    passport = [r for r in _reminders(tmp_path) if r["id"] == "passport-jane"]
    assert [(r["tier"], r["days_left"]) for r in passport] == [("90", 90)]
    assert passport[0]["who"] == "Jane Doe" and passport[0]["kind"] == "passport"
    assert "two photos" in passport[0]["notes"]


def test_shadow_records_nothing_and_writes_no_calendar(tmp_path):
    result = _scan(tmp_path, "2026-10-04", shadow=True)
    assert result.status == "ok"
    assert "Kenya work permit" in result.summary or any(
        "Kenya work permit" in a for a in result.actions
    )
    assert _reminders(tmp_path) == []
    assert not (tmp_path / "out" / "deadlines.ics").exists()


# ---- 3. done closes an occurrence; a recurring one rolls ------------------


def test_done_rolls_a_recurring_obligation_to_its_next_date(tmp_path):
    _scan(tmp_path, "2027-01-02")  # 12-31 is overdue
    result = _done(tmp_path, "annual-report", "2027-01-02")
    assert result.status == "ok"
    _scan(tmp_path, "2027-03-20")  # next is 2027-03-31, 11 days out
    report = [r for r in _reminders(tmp_path) if r["id"] == "annual-report"]
    assert ("2027-03-31", "14") in [(r["due"], r["tier"]) for r in report]
    ics = (tmp_path / "out" / "deadlines.ics").read_bytes().decode()
    assert "DTSTART;VALUE=DATE:20270331" in ics
    assert "DTSTART;VALUE=DATE:20261231" not in ics


def test_done_closes_a_one_off(tmp_path):
    _scan(tmp_path, "2026-10-04")
    _done(tmp_path, "visa-kenya", "2026-10-04")
    _scan(tmp_path, "2026-10-12")  # would be overdue had it not been done
    permit = [r for r in _reminders(tmp_path) if r["id"] == "visa-kenya"]
    assert [r["tier"] for r in permit] == ["7"]
    assert "Kenya work permit" not in (tmp_path / "out" / "deadlines.ics").read_bytes().decode()


def test_done_for_an_unknown_id_is_an_error_that_names_the_ids(tmp_path):
    result = _done(tmp_path, "passprot-jane", "2026-10-04")
    assert result.status == "error"
    assert "passport-jane" in result.summary


# ---- 4. the calendar file ---------------------------------------------------


def test_the_calendar_carries_each_open_occurrence_with_an_alarm_per_window(tmp_path):
    _scan(tmp_path, "2026-10-04")
    ics = (tmp_path / "out" / "deadlines.ics").read_bytes().decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 3
    assert "SUMMARY:Passport renewal (Jane Doe)" in ics
    assert "DTSTART;VALUE=DATE:20270301" in ics
    assert "DTEND;VALUE=DATE:20270302" in ics
    assert "UID:passport-jane-20270301@demo" in ics
    for lead in (90, 30, 7):
        assert f"TRIGGER:-P{lead}D" in ics
    assert "TRIGGER:-P14D" in ics and "TRIGGER:-P3D" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_text_is_escaped_for_the_calendar(tmp_path):
    text = OBLIGATIONS.replace(
        'notes = "Renew at the embassy; two photos."',
        'notes = "Embassy, Nairobi; bring\\nphotos"',
    )
    _scan(tmp_path, "2026-10-04", text=text)
    ics = (tmp_path / "out" / "deadlines.ics").read_bytes().decode()
    assert "Embassy\\, Nairobi\\; bring\\nphotos" in ics.replace("\r\n ", "")


# ---- 5. a bad file names the problem ----------------------------------------


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (
            '[[obligation]]\nid = "a"\ntitle = "x"\ndue = 2026-01-01\n'
            '[[obligation]]\nid = "a"\ntitle = "y"\ndue = 2026-02-01\n',
            "duplicate",
        ),
        ('[[obligation]]\nid = "Bad Id"\ntitle = "x"\ndue = 2026-01-01\n', "id"),
        ('[[obligation]]\nid = "a"\ntitle = "x"\ndue = "soon"\n', "due"),
        ('[[obligation]]\nid = "a"\ntitle = "x"\ndue = 2026-01-01\nevery = "monthly"\n', "every"),
        (
            '[[obligation]]\nid = "a"\ntitle = "x"\ndue = 2026-01-01\nlead_days = [-3]\n',
            "lead_days",
        ),
        ("not toml [", "TOML"),
    ],
)
def test_a_malformed_file_is_an_error_naming_the_problem(tmp_path, text, needle):
    path = tmp_path / "obligations.toml"
    path.write_text(text)
    with pytest.raises(ObligationsError, match=needle):
        load_obligations(path)
    result = _scan(tmp_path, "2026-10-04", text=text)
    assert result.status == "error"


def test_no_obligations_file_is_a_quiet_skip(tmp_path):
    params = {"obligations_file": str(tmp_path / "absent.toml"), "today": "2026-10-04"}
    result = run("demo", "deadlines", "scan", params=params, ledger_dir=tmp_path / "data")
    assert result.status == "ok"
    assert "no obligations file" in result.summary


def test_a_quiet_morning_summary_has_no_dangling_separator(tmp_path):
    result = _scan(tmp_path, "2026-06-01")  # nothing inside a window yet
    assert result.summary == "deadlines 2026-06-01: 3 open, 0 new reminder(s), 0 overdue"
