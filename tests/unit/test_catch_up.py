"""Catch-up (board t-catch-up, chunk 1, 2026-10-14). Caleb arrives at the
sit-down with a year of receipts: some in his camera roll, some as PDFs, a
shoebox to photograph. One link takes the whole pile: many files at once,
from the camera roll or Files, good for a day and for as many sends as he
needs, and a reviewer may ask for it on his behalf. Each receipt lands
unread, and is filed by the RECEIPT'S day once that day is known (the
person's words or what the photo reads), never by the day it was sent;
a foreign one is priced at that day's rate."""

from __future__ import annotations

from datetime import UTC, datetime

from core.tools.receipts import ReceiptDesk, page, take_photo
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_ministry_lines import _door
from tests.unit.test_missionary_month import _cards, _month, feed  # noqa: F401

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
NPR_MAR_5 = "141.03"


def _link(said: dict) -> str:
    return said["show"].split("(", 1)[1].rstrip(")")


def _send(solo, desk, token, data, at=NOW):  # noqa: F811
    return take_photo(
        desk,
        token,
        data,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        now=at,
    )


def _lines(solo, month, person="daniel-reyes"):  # noqa: F811
    people = _door(solo, person).call("month_report", {"month": month})["people"]
    return people[0]["lines"] if people else []


def test_one_pile_link_takes_many_sends_for_a_day():
    now = [1000.0]
    desk = ReceiptDesk(clock=lambda: now[0])
    token = desk.issue_pile("daniel-reyes", "Daniel Reyes", by="daniel-reyes")
    for _ in range(3):
        ticket, why = desk.take(token)
        assert ticket is not None and ticket.pile and why == ""
    now[0] += 24 * 3600 + 1
    assert desk.take(token) == (None, "expired")


def test_the_person_asks_tim_for_a_catch_up_link(solo):  # noqa: F811
    said = _door(solo, "daniel-reyes").call("catch_up_link", {})
    assert said["status"] == "tap_to_add"
    assert said["show"].startswith(
        "[Tap to send your receipts](https://tim.example.org/receipt?many#"
    )
    assert "a day" in said["say"]


def test_a_reviewer_asks_for_the_persons_link_and_a_stranger_cannot(solo):  # noqa: F811
    for_him = _door(solo, "linda-park").call("catch_up_link", {"person": "Daniel Reyes"})
    assert for_him["status"] == "tap_to_add" and "Daniel Reyes" in for_him["say"]
    nobody = _door(solo, "linda-park").call("catch_up_link", {"person": "Nobody Here"})
    assert nobody["status"] == "not_found"


def test_the_pile_page_takes_many_files_from_anywhere():
    html = page("n", many=True)
    assert "multiple" in html and "capture=" not in html
    assert "capture=" in page("n")  # the one-receipt page still opens the camera


def test_a_pile_lands_unread_and_the_link_keeps_working(solo):  # noqa: F811
    desk = ReceiptDesk()
    door = _door(solo, "daniel-reyes")
    door.receipts.desk = desk
    token = _link(door.call("catch_up_link", {})).split("#", 1)[1]
    for n in range(3):
        status, body = _send(solo, desk, token, JPEG + bytes([n]))
        assert status == 200, body
    lines = _lines(solo, "2026-10")
    assert len(lines) == 3
    assert all(line["waiting_on"] == "the receipt's day" for line in lines)


def test_a_receipt_is_filed_by_its_own_day_and_priced_at_that_days_rate(solo, feed):  # noqa: F811
    feed("2026-03-05", npr=NPR_MAR_5)
    _zone(solo, "Asia/Kathmandu")
    desk = ReceiptDesk()
    door = _door(solo, "daniel-reyes")
    door.receipts.desk = desk
    token = _link(door.call("catch_up_link", {})).split("#", 1)[1]
    _send(solo, desk, token, JPEG + b"m")
    [line] = _lines(solo, "2026-10")
    said = door.call(
        "set_receipt_day",
        {"line": line["source"], "said": "that one's from March 5", "day": "2026-03-05"},
    )
    assert said["status"] == "set", said
    assert _lines(solo, "2026-10") == []
    [moved] = _lines(solo, "2026-03")
    assert moved["date"] == "2026-03-05"
    door.call("set_amount", {"line": moved["source"], "said": "4,100 rupees"})
    _month(solo, "2026-03")
    [card] = _cards(solo, "2026-03")
    assert card["lines"].startswith(
        "2026-03-05: $29.07 (NPR 4,100.00 at 141.03 per dollar on 2026-03-05)"
    )


def test_a_day_that_has_not_come_is_refused(solo):  # noqa: F811
    desk = ReceiptDesk()
    door = _door(solo, "daniel-reyes")
    door.receipts.desk = desk
    token = _link(door.call("catch_up_link", {})).split("#", 1)[1]
    _send(solo, desk, token, JPEG + b"f")
    [line] = _lines(solo, "2026-10")
    said = door.call(
        "set_receipt_day", {"line": line["source"], "said": "next year", "day": "2027-01-02"}
    )
    assert said["status"] == "not_a_day"


def test_a_first_name_is_enough(solo):  # noqa: F811
    """Found on the real phone 10/14: "a catch-up link for Ruth" said nobody
    named Ruth files receipts here."""
    for said in ("Daniel", "daniel reyes", "Reyes"):
        got = _door(solo, "linda-park").call("catch_up_link", {"person": said})
        assert got["status"] == "tap_to_add", said
    assert (
        _door(solo, "linda-park").call("catch_up_link", {"person": "Dan"})["status"] == "not_found"
    )
