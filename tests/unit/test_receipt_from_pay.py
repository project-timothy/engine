"""A cash receipt paid from their own pay (board chunk c34, gap C).

A missionary sometimes pays a ministry cost out of their own pay and wants
nothing back. Said so on the receipt, it never rides a card, nobody owes
it, it isn't the ministry's spending, and it comes off their Schedule C on
the tax packet beside the bank lines they marked the same way. Said back
the other way, the receipt goes back on the card.
"""

from __future__ import annotations

from decimal import Decimal

from core.tools.receipts import Claim
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _cards, _door, _line, _month, _snap
from tests.unit.test_missionary_month import NPR_OCT_12, feed  # noqa: F401  (fixture)


def _from_pay(solo, whose: str = "pay") -> dict:  # noqa: F811
    daniel = _door(solo, "daniel-reyes")
    line = _line(daniel)
    return daniel.call(
        "set_category",
        {"line": line["source"], "whose": whose, "said": "I paid that one from my own pay"},
    )


def test_the_word_keeps_its_category_and_says_nobody_owes_it(solo):  # noqa: F811
    _snap(solo, "a $9 taxi to the church", Claim(Decimal("9"), "USD", "$"))
    before = _line(_door(solo, "daniel-reyes"))["category"]
    put = _from_pay(solo)
    assert put["status"] == "placed", put
    assert "nobody owes it back" in put["say"] and "tax packet" in put["say"]
    line = _line(_door(solo, "daniel-reyes"))
    assert line["category"] == before
    assert "from their own pay" in line["paid"]


def test_it_rides_no_card_and_counts_in_no_total(solo):  # noqa: F811
    _snap(solo, "a $9 taxi to the church", Claim(Decimal("9"), "USD", "$"))
    _from_pay(solo)
    result = _month(solo)
    assert result.status != "error", result.summary
    assert [c for c in _cards(solo) if c["status"] == "pending"] == []
    [month] = _door(solo, "daniel-reyes").call("month_report", {"month": "2026-10"})["people"]
    assert month["total"] == "0.00" and month["spent"] == "0.00"


def test_said_back_the_other_way_it_goes_back_on_the_card(solo):  # noqa: F811
    _snap(solo, "a $9 taxi to the church", Claim(Decimal("9"), "USD", "$"))
    _from_pay(solo)
    _from_pay(solo, whose="ministry")
    assert "paid" not in _line(_door(solo, "daniel-reyes"))
    _month(solo)
    [card] = [c for c in _cards(solo) if c["status"] == "pending"]
    assert "$9.00" in card["lines"]


def test_another_word_on_it_later_keeps_it_from_their_pay(solo):  # noqa: F811
    _snap(solo, "a $9 taxi to the church", Claim(Decimal("9"), "USD", "$"))
    _from_pay(solo)
    daniel = _door(solo, "daniel-reyes")
    daniel.call(
        "set_category",
        {"line": _line(daniel)["source"], "category": "Travel", "said": "it was a taxi"},
    )
    assert "from their own pay" in _line(daniel)["paid"]


def test_the_tax_packet_takes_it_as_a_cost(solo):  # noqa: F811
    _snap(solo, "a $9 taxi to the church", Claim(Decimal("9"), "USD", "$"))
    _from_pay(solo)
    got = _door(solo, "daniel-reyes").call("tax_packet", {"year": "2026"})
    assert got["schedule_c"]["costs"] == "9.00"
    assert "paid from their own pay" in got["says"]


def test_a_rupee_receipt_prices_at_its_days_rate_once_the_month_runs(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    _snap(solo, "4,300 rupees for the bus to Pokhara", Claim(Decimal("4300"), "", "rupees"))
    _from_pay(solo)
    daniel = _door(solo, "daniel-reyes")
    waiting = daniel.call("tax_packet", {"year": "2026"})
    assert waiting["schedule_c"]["costs"] == "0.00"
    assert "waits on" in waiting["says"]
    _month(solo)  # the month's run records the day's rate, and still raises no card
    assert [c for c in _cards(solo) if c["status"] == "pending"] == []
    got = daniel.call("tax_packet", {"year": "2026"})
    assert got["schedule_c"]["costs"] == "28.06"


def test_every_person_facing_change_has_its_faq_entry():
    import tomllib
    from pathlib import Path

    faq = tomllib.loads(Path("core/capabilities/faq.toml").read_text(encoding="utf-8"))
    ids = {e["id"] for e in faq["entry"]}
    assert "receipt-from-my-pay" in ids
