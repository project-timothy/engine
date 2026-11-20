"""The books for any span, for every role, at any time (board item t-books;
the owner 2026-10-29: "the monthly, quarterly and annual should be for every
role or position at any time. YTD for example").

Every figure is the month report's own, added up by code: the year report and
the books never disagree, and a person sees only the people their role lets
them see."""

from __future__ import annotations

from decimal import Decimal

from core.ledger import Ledger
from tests.unit.test_bank_catch_up import _both
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_missionary_month import feed  # noqa: F401  (fixture)
from tests.unit.test_who_sees_what import _tools, agency  # noqa: F401  (fixture)

# ---- one missionary on their own ----------------------------------------------------


def test_the_quarter_shows_what_came_in_and_what_was_spent(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    q3 = door.call("books", {"period": "2026-Q3"})
    assert q3["period"] == "Q3 2026"
    assert (q3["from"], q3["through"]) == ("2026-07-01", "2026-09-30")
    assert q3["came_in"] == "250.00"  # the Hollis family's gift on the ministry's account
    assert q3["came_in_by_kind"] == {"Uncategorized": "250.00"}
    assert [m["month"] for m in q3["months"]] == ["2026-07", "2026-08", "2026-09"]
    assert q3["months"][0] == {"month": "2026-07", "came_in": "0.00", "spent": "0.00", "waiting": 0}
    assert q3["says"].startswith("Q3 2026: $250.00 came in and $")
    assert q3["paid_back_to_people"] == "86.40"  # the ministry paying Daniel back moved
    assert q3["for"] == "the whole ministry"  # on his own, he looks over every month


def test_the_books_and_the_year_report_never_disagree(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    [me] = door.call("year_report", {"year": "2026"})["people"]
    books = door.call("books", {"period": "2026"})
    assert books["spent"] == f"{sum(Decimal(m['spent']) for m in me['months']):.2f}"
    assert books["spent_by_kind"] == me["by_category"]
    in_months = sum(Decimal(m["spent"]) for m in books["months"])
    assert f"{in_months:.2f}" == books["spent"]
    gap = Decimal(books["came_in"]) - Decimal(books["spent"])
    assert books["came_in_less_spent"] == f"{gap:.2f}"


def test_year_to_date_is_the_default_and_a_month_reads_alone(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    ytd = door.call("books", {})
    assert ytd["from"] == "2026-01-01" and ytd["period"].startswith("2026 through ")
    july = door.call("books", {"period": "July 2026"})
    assert july["came_in"] == "0.00" and july["spent"] == "0.00"
    assert july["says"] == "July 2026: $0.00 came in and $0.00 was spent."


def test_a_budget_reads_as_of_the_spans_end(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    door.call("set_budget", {"kind": "Travel", "amount": "500", "said": "$500 for travel"})
    june = door.call("books", {"period": "2026-06"})
    [before] = june["budget"]
    assert before["spent"] == "0.00"  # the taxi came in August
    assert before["says"].endswith("(0% of the budget used, 50% of the year gone).")
    [through_q3] = door.call("books", {"period": "Q3 2026"})["budget"]
    assert through_q3["spent"] == "6.80" and through_q3["left"] == "493.20"


def test_words_that_are_no_span_ask_again(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    said = door.call("books", {"period": "next week"})
    assert said["status"] == "needs" and "year to date" in said["say"]


# ---- every role sees their own part ------------------------------------------------


def _spend(agency, person: str, cents: int, day: str = "2026-09-12") -> None:  # noqa: F811
    with Ledger.open(agency["ledger_root"]) as ledger:
        [rid] = ledger.conn.execute(
            "SELECT id FROM expense_report WHERE person = ?", (person,)
        ).fetchone()
        ledger.conn.execute(
            "INSERT INTO expense_line (idempotency_key, report_id, tenant, expense_date, "
            "amount_cents, category, created_at) VALUES (?, ?, 'harvest', ?, ?, 'Meals', 'now')",
            (f"line-{person}", rid, day, cents),
        )
        ledger.conn.commit()


def test_each_role_sees_the_books_of_the_people_it_may_see(agency):  # noqa: F811
    _spend(agency, "Mei Tan", 4200)
    _spend(agency, "Priya Das", 3100)
    finance = _tools(agency, "lydia-brooks").call("books", {"period": "2026-09"})
    assert finance["for"] == "the whole ministry" and finance["spent"] == "73.00"
    ann = _tools(agency, "ann-lee").call("books", {"period": "2026-09"})
    assert ann["for"] == ["Mei Tan"] and ann["spent"] == "42.00"  # Priya is Joel's
    mei = _tools(agency, "mei-tan").call("books", {"period": "Q3"})
    assert mei["for"] == ["Mei Tan"] and mei["spent_by_kind"] == {"Meals": "42.00"}


def test_member_care_is_never_offered_the_books(agency):  # noqa: F811
    offered = {spec.name for spec in _tools(agency, "abby-stone").specs()}
    assert "books" not in offered
