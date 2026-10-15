"""The month walk and "where's my year?" (chunk 1, item 6, 2026-10-14).
After a catch-up, Tim walks the person through a month ("July: 2
receipts, $31.25, here's what I read"); corrections go through the tools
that already exist, in plain words, and one yes agrees to every reading
left. Then the month goes to the reviewer as one card listing every line.
The year tool says which months are done, what still waits, and the
totals by category."""

from __future__ import annotations

from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_missionary_month import _cards, feed  # noqa: F401
from tests.unit.test_reading import _pile


def _read(door, day: str, amount: str, category: str = "Meals") -> None:
    [one] = door.call("receipts_to_read", {"limit": 1})["receipts"]
    door.call(
        "record_reading",
        {
            "line": one["line"],
            "day": day,
            "amount": amount,
            "currency": "USD",
            "category": category,
        },
    )


def test_one_yes_agrees_to_every_reading_in_the_month(solo):  # noqa: F811
    door = _pile(solo, 3)
    _read(door, "2026-07-03", "12.50")
    _read(door, "2026-07-20", "18.75", "Travel")
    _read(door, "2026-06-11", "40.00")
    said = door.call("agree_to_readings", {"month": "2026-07", "said": "yes, July's right"})
    assert said["status"] == "agreed" and said["receipts"] == 2
    [month] = door.call("month_report", {"month": "2026-07"})["people"]
    assert month["total"] == "31.25"
    [june] = door.call("month_report", {"month": "2026-06"})["people"]
    assert june["total"] == "0.00"  # another month's readings wait for their own yes


def test_the_month_goes_to_the_reviewer_as_one_card(solo):  # noqa: F811
    door = _pile(solo, 2)
    _read(door, "2026-07-03", "12.50")
    _read(door, "2026-07-20", "18.75", "Travel")
    door.call("agree_to_readings", {"month": "2026-07", "said": "yes"})
    said = door.call("send_month_for_review", {"month": "2026-07"})
    assert said["status"] == "sent", said
    [card] = _cards(solo, "2026-07")
    assert card["total_cents"] == "3125" and len(card["lines"].split("; ")) == 2


def test_a_month_with_something_still_waiting_says_what(solo):  # noqa: F811
    door = _pile(solo, 2)
    _read(door, "2026-07-03", "12.50")
    _read(door, "2026-07-20", "18.75")
    said = door.call("send_month_for_review", {"month": "2026-07"})
    assert said["status"] == "waiting" and "2 receipts" in said["say"]
    assert _cards(solo, "2026-07") == []


def test_wheres_my_year(solo):  # noqa: F811
    door = _pile(solo, 4)
    _read(door, "2026-07-03", "12.50")
    _read(door, "2026-07-20", "18.75", "Travel")
    _read(door, "2026-06-11", "40.00")
    door.call("agree_to_readings", {"month": "2026-07", "said": "yes"})
    door.call("send_month_for_review", {"month": "2026-07"})
    [me] = door.call("year_report", {"year": "2026"})["people"]
    months = {m["month"]: m for m in me["months"]}
    assert months["2026-07"]["total"] == "31.25" and months["2026-07"]["waiting"] == 0
    assert months["2026-07"]["review"] == "Waiting for approval"
    assert months["2026-06"]["waiting"] == 1
    assert me["not_yet_in_a_month"] == 1
    assert me["by_category"] == {"Meals": "12.50", "Travel": "18.75"}


def test_a_foreign_months_rates_are_fetched_on_the_way_to_the_reviewer(solo, feed):  # noqa: F811
    feed("2026-07-03", npr="140.00")
    door = _pile(solo, 1)
    [one] = door.call("receipts_to_read", {"limit": 1})["receipts"]
    door.call(
        "record_reading",
        {"line": one["line"], "day": "2026-07-03", "amount": "1400", "currency": "NPR"},
    )
    door.call("agree_to_readings", {"month": "2026-07", "said": "yes"})
    assert door.call("send_month_for_review", {"month": "2026-07"})["status"] == "sent"
    [card] = _cards(solo, "2026-07")
    assert card["total_cents"] == "1000"
