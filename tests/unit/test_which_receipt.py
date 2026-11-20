"""Tim asks which receipt rode the card (chunk 5, board t-ask-which-receipt).

The fuzzer's finding (6 stories in 5,000): two rupee receipts a day apart
and within the bank's spread of each other, and one card line. Their
amounts can't tell which one the card paid, so Tim never guesses: neither
receipt is cash or a line of its own, the line waits, and the month asks.
The person's answer puts the one they name behind the line; the other is a
receipt of its own again.

Made-up numbers, as in test_bank_catch_up: the ministry's card paid $31.62
at BHATBHATENI on Aug 7; NPR 4,300 that day and NPR 4,400 on Aug 8 are each
within 6% of it.
"""

from __future__ import annotations

from core.agents.expenses import statements as st
from tests.unit.test_atm_cash import _receipt_on
from tests.unit.test_bank_catch_up import NPR_AUG_7, _both
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_missionary_month import _month, feed  # noqa: F401


def _asked(solo, feed):  # noqa: F811
    feed("2026-08-07", npr=NPR_AUG_7)
    feed("2026-08-08", npr=NPR_AUG_7)
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _both(solo)
    _receipt_on(door, desk, token, solo, "2026-08-07", "4,300 rupees", JPEG + b"card")
    _receipt_on(door, desk, token, solo, "2026-08-08", "4,400 rupees", JPEG + b"cash")
    _month(solo, "2026-08")  # records the days' rates, so both can be matched
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    bhat = next(b for b in month["bank"] if b["words"].startswith("BHATBHATENI"))
    return door, month, bhat


def test_two_receipts_that_could_each_be_the_card_line_are_asked_about(solo, feed):  # noqa: F811
    _door, month, bhat = _asked(solo, feed)
    assert bhat["waiting_on"] == st.WAITING_WHICH and bhat["receipt"] is None
    assert [r["said"] for r in bhat["could_be"]] == ["NPR 4,300.00", "NPR 4,400.00"]
    assert month["lines"] == []  # neither is owed back, priced, or cash while Tim asks


def test_their_answer_puts_the_one_they_name_behind_the_line(solo, feed):  # noqa: F811
    door, _month_was, bhat = _asked(solo, feed)
    card = next(r for r in bhat["could_be"] if r["said"] == "NPR 4,300.00")
    said = door.call(
        "pick_card_receipt",
        {"line": bhat["source"], "receipt": card["source"], "said": "the 4,300 one was the card"},
    )
    assert said["status"] == "set", said
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    bhat = next(b for b in month["bank"] if b["words"].startswith("BHATBHATENI"))
    assert bhat["receipt"] == "attached" and bhat["waiting_on"] is None
    assert "could_be" not in bhat
    [other] = month["lines"]
    assert other["said"] == "NPR 4,400.00"


def test_only_a_receipt_tim_asked_about_can_be_named(solo, feed):  # noqa: F811
    door, _month_was, bhat = _asked(solo, feed)
    said = door.call(
        "pick_card_receipt", {"line": bhat["source"], "receipt": "events#1", "said": "that one"}
    )
    assert said["status"] == "asks"
    assert (
        door.call("pick_card_receipt", {"line": "bank#0000", "receipt": "x", "said": "y"})["status"]
        == "not_found"
    )


def test_the_answer_is_recorded_as_their_words():
    assert st.RODE_EVENT == "expense.receipt_rode"
    log = [{"event_type": st.RODE_EVENT, "payload": {"sha256": "r", "line": "l1"}}]
    log.append({"event_type": st.RODE_EVENT, "payload": {"sha256": "r", "line": "l2"}})
    assert st.rode(log) == {"r": "l2"}  # the latest they said
