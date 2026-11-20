"""The missionary's fund (board chunk c31, t-missionary-fund; the owner,
2026-11-12: "Money given for a missionary lands in that missionary's own
fund inside the ministry's books, never in the ministry's income").

The fund is the person's own account in the journal
(``Liabilities:People:<person>``), the same running account that already
holds what the ministry owes them and what they owe it. What was missing is
the deposit: a line of money in that was the person's own (their pay from a
support platform landing in the ministry's account, the EquipNet lesson of
2026-11-12, or a gift given for them) credits their fund. It is never the
ministry's income and never its gross receipts for a 990. A deposit whose
words name a person (a payroll deposit's individual name) says so, so Tim
asks whether it is their pay."""

from __future__ import annotations

from core.agents.expenses import statements as st
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_books_journal import RUTH, _month, _post, _row
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

DANIEL = "Daniel Reyes"
PAYROLL = "EQUIPNET PPD ID: 1234 Descr:Payroll Sec:PPD Ind Name:Daniel Reyes"
GIFT = "ONLINE GIFT HOLLIS FAMILY"
GUEST_HOUSE = "KATHMANDU GUEST HOUSE"

FUND_CSV = f"""Living Water Ministry Checking ...4417
Statement period,08/01/2026 - 08/31/2026

Date,Description,Debit,Credit,Balance
08/03/2026,{PAYROLL},,400.00,"5,400.00"
08/05/2026,{GIFT},,250.00,"5,650.00"
08/20/2026,{GUEST_HOUSE},"1,204.50",,"4,445.50"
,Ending balance,,,"4,445.50"
"""


def test_their_own_money_in_credits_their_fund_never_income():
    got = _post(_month(bank=[_row(st.THEIRS_IN, "400.00", st.BANK)]))
    assert got == {"Assets:Bank:Ruth Okafor": 40000, f"Liabilities:People:{RUTH}": -40000}


def test_their_own_money_in_on_their_own_account_is_never_posted():
    assert _post(_month(bank=[_row(st.THEIRS_IN, "400.00", st.PERSON)])) == {}


def test_the_names_a_deposit_carries_are_read_from_its_words():
    assert st.names_a_person(PAYROLL) == DANIEL
    assert st.names_a_person("ACH CREDIT IND NAME: caleb meer TRN: 0042") == "caleb meer"
    assert st.names_a_person(GIFT) == ""


# ---- through the door ------------------------------------------------------------------


def _bank(door) -> list[dict]:
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    return me["bank"]


def _line(door, words: str) -> dict:
    return next(b for b in _bank(door) if b["words"] == words)


def test_a_deposit_naming_a_person_says_so(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, FUND_CSV)
    assert _line(door, PAYROLL)["names"] == DANIEL
    assert "names" not in _line(door, GIFT)


def test_a_deposit_said_to_be_theirs_lands_in_their_fund(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, FUND_CSV)
    put = door.call(
        "explain_bank_line",
        {"line": _line(door, PAYROLL)["source"], "said": "my EquipNet pay", "whose": "own"},
    )
    assert put["status"] == "set" and "their own money" in put["say"]
    assert _line(door, PAYROLL)["role"] == st.THEIRS_IN
    door.call(
        "explain_bank_line",
        {"line": _line(door, GUEST_HOUSE)["source"], "said": "mine", "whose": "own"},
    )
    books = door.call("books", {"period": "2026-Q3"})
    de = books["double_entry"]
    assert de["balanced"] is True and de["ties_to_the_bank"] is True
    assert de["accounts_at_end"]["Assets:Bank:Daniel Reyes"] == "4445.50"
    # The ministry's income is the gift alone; his pay is his.
    assert sum(float(v) for v in de["income_by_kind"].values()) == 250.0
    assert de["income_by_kind"] == books["came_in_by_kind"]
    # In 400.00 of his own, out 1,204.50 spent on himself: he owes 804.50 (a
    # liability read as a credit, so what he owes the ministry reads negative).
    assert de["accounts_at_end"][f"Liabilities:People:{DANIEL}"] == "-804.50"
    # The books' own summary says the same: what he owes, net of his fund.
    assert books["owed_to_the_ministry"] == "804.50" and "held_for_people" not in books


def test_their_fund_with_nothing_drawn_is_money_held_for_them(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, FUND_CSV)
    door.call(
        "explain_bank_line",
        {"line": _line(door, PAYROLL)["source"], "said": "my pay", "whose": "own"},
    )
    books = door.call("books", {"period": "2026-Q3"})
    assert books["owed_to_the_ministry"] == "0.00"
    assert books["held_for_people"] == {DANIEL: "400.00"}


def test_their_own_money_in_is_never_the_ministrys_gross_receipts(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, FUND_CSV)
    door.call(
        "explain_bank_line",
        {"line": _line(door, PAYROLL)["source"], "said": "my pay", "whose": "own"},
    )
    from core.tools.catalog import _log

    assert st.came_in(_log(door), "2026-01-01", "2026-12-31") == 25000
