"""A receipt that shows no day it was paid (the owner's call of 2026-11-19:
"ask on the ticket"): a train or ferry ticket prints only the day of travel,
which is not the day she paid, so the reader leaves the day blank
(core/tools/receipt_read.py CURRENCY) and Tim asks her the day."""

from __future__ import annotations

from decimal import Decimal

from core.tools import receipt_read
from core.tools.receipts import Claim, page
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_receipt_pile import _pile
from tests.unit.test_receipt_read import _reads, _shoprite, _snap, _tap

WHAT = "Indian Railways, 1,830 rupees"


def _ticket() -> dict:
    return _shoprite(
        amount="1830.00", currency="INR", vendor_name="Indian Railways", invoice_date=None
    )


def test_the_reader_is_told_a_travel_date_is_not_the_day_paid():
    assert "Journey, Travel date, Reisedatum" in receipt_read.CURRENCY
    assert "reply null" in receipt_read.CURRENCY


def test_a_ticket_with_no_day_paid_is_read_back_and_the_day_asked(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _ticket())
    desk, body = _snap(solo)
    lines = receipt_read.words()
    assert body["say"] == lines["reads_no_day"].format(what=WHAT)
    status, said, _ = _tap(solo, desk, body, "right")
    assert status == 200 and said["say"] == lines["counted_no_day"]


def test_a_ticket_she_named_the_amount_of_still_gets_the_day_asked(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _ticket())
    _desk, body = _snap(solo, Claim(Decimal("1830"), "INR", "rupees"), said="1830 rupees")
    # rupees: its dollar amount is set with the monthly report, as always
    assert body["say"].startswith(receipt_read.words()["same_no_day"].format(what=WHAT))


def test_a_receipt_with_its_day_is_read_back_as_always(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    desk, body = _snap(solo)
    lines = receipt_read.words()
    assert body["say"] == lines["reads"].format(what="Shoprite, November 12, 4,300 rupees")
    assert _tap(solo, desk, body, "right")[1]["say"] == lines["counted"]


def test_in_a_pile_a_ticket_is_marked_and_the_day_asked_after_her_yes(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _ticket())
    _, [body] = _pile(solo, JPEG)
    lines = receipt_read.words()
    assert body["read"] == WHAT + lines["no_day_mark"] and body["no_day"] is True
    on = page("n", many=True, reads=True)
    assert "noDay" in on and lines["pile_no_day"] in on and lines["pile_no_day_one"] in on


def test_the_no_day_words_are_plain():
    lines = receipt_read.words()
    for key in ("reads_no_day", "counted_no_day", "same_no_day", "no_day_mark", "pile_no_day"):
        assert "—" not in lines[key] and "card" not in lines[key].lower()
    assert "day" in lines["counted_no_day"] and "Tim" in lines["counted_no_day"]
