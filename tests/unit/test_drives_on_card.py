"""Drives on the month's card (the owner, 2026-10-14). A ministry drive rides
the month's one yes as a line in the reviewer's words: "Oct 12: 14.0
miles, visiting the church in Pokhara, $10.15 at 72.5 cents a mile",
priced at the rate in force on its own day. A personal drive, or one
nobody has spoken for, never becomes money. The approved month files each
drive as a report line, and a drive is a record like a receipt: approved
stays approved."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_expense_records import _approve, _lines
from tests.unit.test_mileage_purpose import _drive, _drives, _say
from tests.unit.test_missionary_month import _cards, _month, _reviewer


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_a_ministry_drive_is_a_line_on_the_months_card(solo):  # noqa: F811
    _drive(solo)
    _say(solo, "visiting the church in Pokhara")
    _month(solo)
    [card] = _cards(solo)
    assert card["lines"] == (
        "2026-10-12: 14.0 miles, visiting the church in Pokhara, $10.15 at 72.5 cents a mile"
    )
    assert card["total_cents"] == "1015"


def test_personal_and_unexplained_drives_never_become_money(solo):  # noqa: F811
    _drive(solo)
    _say(solo, "that one was personal")
    _drive(solo, start="2026-10-13T03:00:00+00:00")
    _month(solo)
    assert _cards(solo) == []


def test_the_approved_month_files_the_drive_once(solo):  # noqa: F811
    _drive(solo)
    _say(solo, "visiting the church in Pokhara; then home")
    _month(solo)
    [card] = _cards(solo)
    assert "Pokhara, then home" in card["lines"]  # one line, never split in two
    _approve(solo, card, at=_now())
    _month(solo)
    [line] = _lines(solo)
    assert line["amount_cents"] == 1015 and line["expense_date"] == "2026-10-12"
    assert "Pokhara" in line["note"]
    [month] = _reviewer(solo).call("month_report", {"month": "2026-10"})["people"]
    assert month["drives"] == [] and month["total"] == "10.15"
    _month(solo)
    assert len(_lines(solo)) == 1 and len(_cards(solo)) == 1


def test_a_new_word_after_the_yes_asks_again(solo):  # noqa: F811
    _drive(solo, miles="20.0")
    _say(solo, "hospital visit")
    _month(solo)
    [card] = _cards(solo)
    _approve(solo, card, at=_now())
    _say(solo, "hospital visit and the pastor's house")
    _month(solo)
    assert _lines(solo) == []
    assert [c["status"] for c in _cards(solo)] == ["approved", "pending"]
    assert "pastor's house" in _cards(solo)[-1]["lines"]
    assert _drives(solo)[0]["for"] == "hospital visit and the pastor's house"
