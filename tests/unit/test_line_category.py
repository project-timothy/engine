"""An explained bank line gets its category, and the ministry's money spent
on the person is owed to it (Caleb's first night, 2026-10-25: 44 lines
explained, 40 left with no category, and three supermarket and clinic
charges on the ministry's card he called his own counted as the ministry's
spending).

Tim proposes a category from the person's words and says which; when the
words point nowhere he names the ministry's list and asks, and the answer
rides back as ``category``. A line said to be their own (``whose`` own) is
never spending: it is owed to the ministry, off the card's total."""

from __future__ import annotations

from tests.unit.test_bank_catch_up import _both, _events
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_missionary_month import _cards, feed  # noqa: F401

GUEST_HOUSE = "KATHMANDU GUEST HOUSE"


def _line(door, words: str) -> dict:
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    return next(b for b in me["bank"] if b["words"] == words)


def test_words_that_point_nowhere_name_the_list_and_ask(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    said = door.call(
        "explain_bank_line", {"line": _line(door, GUEST_HOUSE)["source"], "said": "for the work"}
    )
    assert said["status"] == "set"
    assert said["category"] == ""
    assert "Ministry Supplies" in said["say"] and "pass it as category" in said["say"]
    assert _line(door, GUEST_HOUSE)["category"] is None


def test_a_category_said_back_places_the_line(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    source = _line(door, GUEST_HOUSE)["source"]
    door.call("explain_bank_line", {"line": source, "said": "for the work"})
    put = door.call(
        "explain_bank_line",
        {"line": source, "said": "for the work", "category": "ministry supplies"},
    )
    assert put["status"] == "set" and put["category"] == "Ministry Supplies"
    assert "under Ministry Supplies" in put["say"]
    assert _line(door, GUEST_HOUSE)["category"] == "Ministry Supplies"


def test_a_category_off_the_list_writes_nothing(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    before = len(_events(solo, "bank.line_purpose"))
    put = door.call(
        "explain_bank_line",
        {"line": _line(door, GUEST_HOUSE)["source"], "said": "rent", "category": "Yachts"},
    )
    assert put["status"] == "needs_category" and "Ministry Supplies" in put["say"]
    assert len(_events(solo, "bank.line_purpose")) == before


def test_words_that_point_one_way_still_say_which(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    put = door.call(
        "explain_bank_line",
        {"line": _line(door, GUEST_HOUSE)["source"], "said": "the team's hotel in Kathmandu"},
    )
    assert put["category"] == "Lodging" and "under Lodging" in put["say"]


def test_their_own_on_the_ministrys_card_is_owed_to_the_ministry(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    put = door.call(
        "explain_bank_line",
        {
            "line": _line(door, GUEST_HOUSE)["source"],
            "said": "that was mine, not the ministry's",
            "whose": "own",
        },
    )
    assert put["status"] == "set" and "owed to the ministry" in put["say"]
    row = _line(door, GUEST_HOUSE)
    assert row["role"] == "owed to the ministry" and row["waiting_on"] is None
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    assert me["owed_to_ministry"] == "1204.50"
    for b in me["bank"]:
        if b["waiting_on"]:
            door.call("explain_bank_line", {"line": b["source"], "said": "printing"})
    door.call("send_month_for_review", {"month": "2026-08"})
    [card] = [c for c in _cards(solo, "2026-08") if "lines" in c]
    assert GUEST_HOUSE not in card["lines"]
    assert card["repay"] == f"2026-08-20: $1,204.50 owed to the ministry ({GUEST_HOUSE})"


def test_their_own_on_their_own_account_is_refused(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    taxi = _line(door, "TAXI KATHMANDU")
    put = door.call("explain_bank_line", {"line": taxi["source"], "said": "mine", "whose": "own"})
    assert put["status"] == "asks"


def test_cash_said_for_takes_the_category_they_agree_to(solo, feed):  # noqa: F811
    from tests.unit.test_cash_count import _with_cash

    door, _desk, _token = _with_cash(solo)
    door.call("count_cash", {"said": "I have 50,000 rupees left"})
    off = door.call(
        "explain_cash", {"said": "2,000 rupees for the work", "whose": "ministry", "category": "x"}
    )
    assert off["status"] == "needs_category" and "Ministry Supplies" in off["say"]
    put = door.call(
        "explain_cash",
        {"said": "2,000 rupees for the work", "whose": "ministry", "category": "Travel"},
    )
    assert put["status"] == "set" and "under Travel" in put["say"], put
    vague = door.call("explain_cash", {"said": "the rest for the work", "whose": "ministry"})
    assert "pass it as category" in vague["say"], vague
    [month] = door.call("month_report", {"month": "2026-10"})["people"]
    cash = [b for b in month["bank"] if str(b["words"]).startswith("Cash with no receipt")]
    assert sorted(str(b["category"]) for b in cash) == [
        "Other",
        "Travel",
    ]  # no category: Other, as before
