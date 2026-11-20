"""The books in double entry (board chunk c28, t-books-ledger; the owner,
2026-11-12: a thin accounting layer inside Tim for small ministries).

Every row the month report already gives becomes a balanced journal entry,
by code: receipts owed back, the ministry's bank lines, ATM cash and what it
bought, money sent to the person, paying them back, and ministry money they
spent on themselves. Debits equal credits for every span, and the bank's
account in the journal is the balance the bank itself gave."""

from __future__ import annotations

from core.agents.expenses import journal as j
from core.agents.expenses import statements as st
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_statements import MINISTRY_CSV

RUTH = "Ruth Okafor"


def _month(lines=(), bank=()) -> list[dict]:
    return [{"person": RUTH, "lines": list(lines), "bank": list(bank)}]


def _row(role: str, dollars: str | None, paid_from: str, category: str = "", **more) -> dict:
    return {
        "date": "2026-10-03",
        "words": "a line",
        "dollars": dollars,
        "role": role,
        "category": category or None,
        "paid_from": paid_from,
        "source": "bank#abc",
        **more,
    }


def _post(people: list[dict]) -> dict[str, int]:
    entries, _waiting = j.month_entries(people)
    assert j.check(entries).balanced
    return j.totals(entries)


def test_a_receipt_they_paid_is_spending_owed_back_to_them():
    got = _post(_month(lines=[{"date": "2026-10-02", "dollars": "12.50", "category": "Meals"}]))
    assert got == {"Expenses:Meals": 1250, f"Liabilities:People:{RUTH}": -1250}


def test_a_receipt_the_ministrys_cash_paid_owes_back_only_their_part():
    line = {"dollars": "20.00", "category": "Travel", "paid": "...", "owed_back": "5.00"}
    got = _post(_month(lines=[line]))
    assert got == {
        "Assets:Cash:Ruth Okafor": -1500,
        "Expenses:Travel": 2000,
        f"Liabilities:People:{RUTH}": -500,
    }


def test_each_kind_of_bank_line_lands_where_its_money_went():
    bank = [
        _row(st.CAME_IN, "500.00", st.BANK, "Donations"),
        _row(st.MINISTRY_PAID, "40.00", st.BANK, "Printing"),
        _row(st.CASH, "100.00", st.BANK),
        _row(st.MINISTRY_PAID, "30.00", st.CASH_IN_HAND, "Travel"),
        _row(st.SENT, "200.00", st.BANK),
        _row(st.MINISTRY_PAID, "150.00", st.PERSON, "Scholarships"),  # from the money sent
        _row(st.REPAY, "10.00", st.BANK),
        _row(st.PAID_BACK, "25.00", st.BANK),
        _row(st.OWED, "25.00", st.PERSON, "Meals"),
    ]
    got = _post(_month(bank=bank))
    assert got == {
        "Assets:Bank:Ruth Okafor": 50000 - 4000 - 10000 - 20000 - 1000 - 2500,
        "Assets:Cash:Ruth Okafor": 10000 - 3000,
        "Expenses:Meals": 2500,
        "Expenses:Printing": 4000,
        "Expenses:Scholarships": 15000,
        "Expenses:Travel": 3000,
        "Income:Donations": -50000,
        # held 200 sent, spent 150 of it; owes 10; paid back 25 for 25 owed
        f"Liabilities:People:{RUTH}": 20000 - 15000 + 1000 + 2500 - 2500,
    }


def test_their_own_accounts_mirror_lines_are_never_posted_twice():
    bank = [
        _row(st.PAID_BACK, "25.00", st.PERSON),  # the paying back, seen on their account
        _row(st.CASH, "60.00", st.PERSON),  # their own cash, their own money
        _row(st.SENT, "80.00", st.PERSON),
    ]
    entries, _ = j.month_entries(_month(bank=bank))
    assert entries == []


def test_a_line_still_waiting_on_its_amount_is_counted_never_guessed():
    entries, waiting = j.month_entries(
        _month(lines=[{"dollars": None}], bank=[_row(st.MINISTRY_PAID, None, st.BANK)])
    )
    assert (entries, waiting) == ([], 2)


def test_a_line_with_no_kind_reads_uncategorized_as_the_books_say():
    got = _post(_month(bank=[_row(st.MINISTRY_PAID, "9.99", st.BANK)]))
    assert got["Expenses:Uncategorized"] == 999


def test_an_unbalanced_entry_is_named():
    bad = j.Entry("2026-10-01", "half a line", (("Expenses:Meals", 100),), "x")
    found = j.check([bad])
    assert not found.balanced and found.unbalanced == (bad,)


def test_what_people_are_owed_and_what_they_hold_read_from_their_accounts():
    to, by = j.owed({"Liabilities:People:A": -500, "Liabilities:People:B": 300, "Income:X": -1})
    assert (to, by) == ({"A": 500}, {"B": 300})


# ---- through the door: the sample ministry's statement -------------------------------


def test_the_books_balance_in_double_entry_and_tie_to_the_bank(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    q3 = door.call("books", {"period": "2026-Q3"})
    de = q3["double_entry"]
    assert de["balanced"] is True and de["debits"] == de["credits"]
    assert de["ties_to_the_bank"] is True
    at_end = de["accounts_at_end"]
    assert at_end["Assets:Bank:Daniel Reyes"] == q3["in_the_bank"]["balance"] == "3374.74"
    assert at_end["Equity:Opening"] == "5000.00"
    # The journal's income and spending are the books' own, kind by kind.
    assert de["income_by_kind"] == q3["came_in_by_kind"]
    assert de["spending_by_kind"] == q3["spent_by_kind"]


def test_a_months_journal_carries_the_balances_from_before_it(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    september = door.call("books", {"period": "September 2026"})["double_entry"]
    assert september["accounts_at_end"]["Assets:Bank:Daniel Reyes"] == "3374.74"
    assert september["entries"] == 2  # the two Staples lines, not August's
