"""The year's budget (board item t-year-budget; the owner 2026-10-15: small,
micro and one-missionary ministries first). The person who looks over every
month says it in plain words; Tim reads it back beside what was spent."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.agents.expenses.budget import latest, standing, year_gone
from core.tools.receipts import Claim
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door, _month, _snap

# ---- the arithmetic ------------------------------------------------------------------


def test_the_last_budget_said_for_a_kind_and_year_wins():
    said = [
        {"year": "2026", "kind": "Scholarships", "cents": 1_000_000},
        {"year": "2025", "kind": "Scholarships", "cents": 500_000},
        {"year": "2026", "kind": "Scholarships", "cents": 1_200_000},
    ]
    assert latest(said, "2026") == {"Scholarships": 1_200_000}


def test_how_much_of_the_year_is_gone():
    assert year_gone("2026", date(2026, 1, 1)) == 0
    assert year_gone("2026", date(2026, 10, 15)) == 79
    assert year_gone("2025", date(2026, 10, 15)) == 100
    assert year_gone("2027", date(2026, 10, 15)) == 0


def test_each_budget_reads_as_spent_left_and_the_year_gone():
    [line] = standing(
        {"Scholarships": 1_200_000},
        {"Scholarships": 945_000},
        year="2026",
        today=date(2026, 10, 15),
    )
    assert line == {
        "kind": "Scholarships",
        "budget": "12000.00",
        "spent": "9450.00",
        "left": "2550.00",
        "says": "Scholarships: $9,450.00 spent of $12,000.00, $2,550.00 left "
        "(79% of the budget used, 79% of the year gone).",
    }


def test_over_budget_says_over_and_a_past_year_drops_the_pace():
    [line] = standing({"Meals": 10_000}, {"Meals": 12_500}, year="2025", today=date(2026, 10, 15))
    assert line["says"] == "Meals: $125.00 spent of $100.00, $25.00 over."
    assert line["left"] == "-25.00"


# ---- through the door ----------------------------------------------------------------


def test_the_missionary_on_their_own_sets_it_and_hears_whats_left(solo):  # noqa: F811
    tim = _door(solo, "daniel-reyes")
    assert "set_budget" in {s.name for s in tim.specs()}
    said = tim.call(
        "set_budget", {"kind": "Meals", "amount": "$1,200", "said": "our meal budget is $1,200"}
    )
    assert said["status"] == "set"
    assert said["say"].startswith("Your 2026 budget for Meals is $1,200.00.")
    _snap(solo, "$52 for dinner with the pastors", Claim(Decimal("52"), "USD", "$"))
    _month(solo)
    year = tim.call("year_report", {"year": "2026"})
    [meals] = year["budget"]
    assert meals["spent"] == "52.00" and meals["left"] == "1148.00"
    assert meals["says"].startswith("Meals: $52.00 spent of $1,200.00, $1,148.00 left")


def test_saying_it_again_replaces_it_and_the_reviewer_may_too(solo):  # noqa: F811
    _door(solo, "daniel-reyes").call(
        "set_budget", {"kind": "Meals", "amount": "1200", "said": "meals $1,200"}
    )
    linda = _door(solo, "linda-park")
    assert (
        linda.call("set_budget", {"kind": "meals", "amount": "1500", "said": "make it $1,500"})[
            "status"
        ]
        == "set"
    )
    [meals] = linda.call("year_report", {"year": "2026"})["budget"]
    assert meals["budget"] == "1500.00"


def test_a_kind_the_words_do_not_settle_is_the_ais_to_pick(solo):  # noqa: F811
    said = _door(solo, "daniel-reyes").call(
        "set_budget", {"kind": "gifts", "amount": "300", "said": "gifts $300"}
    )
    assert said["status"] == "needs"
    assert said["pick_kind"].startswith("Choose the closest of these yourself")


def test_no_budget_set_leaves_the_year_report_as_it_was(solo):  # noqa: F811
    assert "budget" not in _door(solo, "daniel-reyes").call("year_report", {"year": "2026"})


def test_a_year_that_is_not_a_year_is_asked_for_again_and_nothing_is_set(solo):  # noqa: F811
    # Review 2026-11-10: "26" or "next year" came back "set" while the books
    # kept nothing, so the person was told a budget existed that did not.
    tim = _door(solo, "daniel-reyes")
    for year in ("26", "next year", "20266"):
        said = tim.call(
            "set_budget", {"kind": "Meals", "amount": "1200", "year": year, "said": "meals"}
        )
        assert said["status"] == "needs", year
        assert "year" in said["say"]
    assert tim.call("year_report", {"year": "2026"}).get("budget", []) == []
