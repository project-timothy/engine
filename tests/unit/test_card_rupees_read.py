"""A rupee receipt paid by the ministry's card, read in a catch-up (the
alpha dress rehearsal, 2026-10-15).

The receipt can find its card line only at the day's rate, and the rate was
fetched only when the month was sent. Before that the card line waited on
its receipt, so the month would not send; the receipt was priced as cash
from the ATM envelope; and once sent, the month owed the person back for a
purchase the ministry's card had already paid. The rate is now recorded the
moment a receipt in another currency has both its day and its amount.

Made-up numbers: NPR 4,300 at Bhat-Bhateni on Aug 7; the ministry's card
was charged $31.62; NPR 136.0 to the dollar that day (4,300 / 136 = 31.62).
"""

from __future__ import annotations

from core.tools.receipts import ReceiptDesk
from tests.unit.test_catch_up import NOW, _link, _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_ministry_lines import _door
from tests.unit.test_missionary_month import feed  # noqa: F401  (fixture)

CARD_CSV = """Date,Description,Amount
2026-08-03,ONLINE GIFT HOLLIS FAMILY,250.00
2026-08-07,BHATBHATENI SUPERMARKET KATHMANDU,-31.62
"""


def _read_card_receipt(solo, feed):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    feed("2026-08-07", npr="136.0")
    desk = ReceiptDesk()
    door = _door(solo, "daniel-reyes")
    door.receipts.desk = desk
    token = _link(door.call("catch_up_link", {})).split("#", 1)[1]
    _send(solo, desk, token, CARD_CSV.encode())
    _send(solo, desk, token, JPEG + b"bhatbhateni", at=NOW)
    [held] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    [one] = door.call("receipts_to_read", {})["receipts"]
    read = door.call(
        "record_reading",
        {
            "line": one["line"],
            "day": "2026-08-07",
            "amount": "4300",
            "currency": "NPR",
            "payee": "Bhat-Bhateni Supermarket",
        },
    )
    assert read["status"] == "read", read
    door.call("agree_to_readings", {"month": "2026-08", "said": "yes, that's right"})
    return door


def _card_line(door) -> dict:
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    return next(b for b in month["bank"] if "BHATBHATENI" in b["words"]), month


def test_a_read_rupee_receipt_finds_its_card_line_before_the_month_is_sent(solo, feed):  # noqa: F811
    door = _read_card_receipt(solo, feed)
    line, month = _card_line(door)
    assert line["receipt"] == "attached" and line["waiting_on"] is None
    assert month["bank_waiting"] == 0
    assert month["lines"] == []  # the receipt rides its card line; it is no cash spending
    assert "cash_on_hand" not in month


def test_the_month_sends_and_owes_nothing_back_for_the_cards_purchase(solo, feed):  # noqa: F811
    door = _read_card_receipt(solo, feed)
    sent = door.call("send_month_for_review", {"month": "2026-08"})
    assert sent["status"] == "sent", sent
    assert "$0.00 owed back" in sent["say"]
    [report] = [r for r in door.call("expense_reports", {})["rows"] if r["month"] == "2026-08"]
    assert report["total"] == "0.00"


def test_a_card_paid_receipt_is_split_through_its_bank_line(solo, feed):  # noqa: F811
    """Riding its card line, the receipt left the month's receipts, and the
    person's AI had no way to name it to split off their part."""
    door = _read_card_receipt(solo, feed)
    line, _month = _card_line(door)
    assert line["receipt_source"].startswith("events#")
    said = door.call(
        "split_line",
        {"line": line["receipt_source"], "said": "1,300 rupees was mine", "whose": "own"},
    )
    assert said["status"] == "set", said
    _line, month = _card_line(door)
    assert month["owed_to_ministry"] == "9.56"  # 1,300 of 4,300 of the card's $31.62


def test_a_month_the_ministrys_card_paid_reads_as_spent_with_nothing_owed_back(solo, feed):  # noqa: F811
    """The second model run: the year's "total" was what was owed back, so a
    month the ministry's own money paid read "$0.00" and the person's AI
    said October had nothing in it."""
    door = _read_card_receipt(solo, feed)
    _line, month = _card_line(door)
    assert (month["spent"], month["total"]) == ("31.62", "0.00")
    [me] = door.call("year_report", {"year": "2026"})["people"]
    august = next(m for m in me["months"] if m["month"] == "2026-08")
    assert (august["spent"], august["owed_back"]) == ("31.62", "0.00")
    assert "total" not in august


def test_the_split_tool_tells_tim_rupees_off_a_dollar_line_go_on_its_receipt():
    """t-s08-rupees (voice gate 2026-11-20, S08 1 of 5): Tim said he couldn't
    split 430 rupees off a $32.85 charge and asked for dollars. The test
    above is the tool doing it; this is the tool saying so to the model."""
    from core.tools.bank import SPLIT_LINE

    # Tim's own voice reads the short form; the door's words stay pinned.
    assert (
        "receipt_source for rupees off a dollar charge (never ask for dollars)" in SPLIT_LINE.short
    )
