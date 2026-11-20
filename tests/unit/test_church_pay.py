"""What a ministry paid a person as pay in a year, counted once (gap A of the
11/15 plan; board chunk c34, Session 5).

The ministry's side records pay as a line out of its own account with the
role "their pay" and whom it paid. The person's packet used to count only
deposits on their own account matched to a payer by the bank's words, so a
missionary whose own statement wasn't on the box showed nothing from the
ministry that paid them. One function now answers "what this ministry paid
this person as pay this year"; the packet reads it, and a deposit on the
person's own account that carries the ministry's name is that same money,
shown beside it and never added."""

from __future__ import annotations

from core.agents.expenses import church_pay, payers
from core.agents.expenses import statements as st
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_pay_day import _line, _row, _set_pay
from tests.unit.test_statements import MINISTRY_CSV

MINISTRY = "Living Water Mozambique, Inc."


def _rows() -> list[dict]:
    return [
        {"date": "2026-08-12", "dollars": "-2000.00", "role": st.PAY_OUT, "paid_to": "ruth"},
        {"date": "2026-09-12", "dollars": "-2000.00", "role": st.PAY_OUT, "paid_to": "ruth"},
        {"date": "2026-09-12", "dollars": "-500.00", "role": st.PAY_OUT, "paid_to": "abe"},
        {"date": "2025-12-12", "dollars": "-2000.00", "role": st.PAY_OUT, "paid_to": "ruth"},
        {"date": "2026-08-20", "dollars": "-90.00", "role": st.OWED, "paid_to": "ruth"},
    ]


def test_pay_is_the_their_pay_lines_for_that_person_in_that_year():
    assert church_pay.paid_as_pay(_rows(), "ruth", "2026") == [
        {"date": "2026-08-12", "dollars": "2000.00"},
        {"date": "2026-09-12", "dollars": "2000.00"},
    ]
    assert church_pay.by_person(_rows(), "2026") == {"abe": 50000, "ruth": 400000}


def test_a_matching_deposit_on_their_own_account_is_the_same_money_shown_once():
    listed = [payers.Payer.of({"person": "ruth", "name": "Grace Fellowship", "kind": "church",
                               "words": "GRACE FELLOWSHIP"})]  # fmt: skip
    mine = [
        {
            "date": "2026-08-13",
            "dollars": "2000.00",
            "role": st.THEIRS_IN,
            "words": "GRACE FELLOWSHIP PAYROLL",
        },  # fmt: skip
    ]
    pay = church_pay.paid_as_pay(_rows(), "ruth", "2026")
    listed, got = payers.sort_with_pay(mine, listed, ("Grace Fellowship", pay), "ruth")
    out, says = payers.rows(listed, got, {}, {}, ("ruth", "2026"))
    [church] = out
    assert church["came_in_seen"] == "4000.00"  # the books' two lines, never 6,000
    assert church["paid_as_pay"] == pay
    assert church["deposits"] == [{"date": "2026-08-13", "dollars": "2000.00"}]
    assert "the same money" in says[0]


def test_with_no_statement_of_theirs_the_ministry_is_their_payer(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    put = daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "his pay", "pay_of": "Daniel Reyes"},
    )
    assert put["status"] == "set", put
    got = daniel.call("tax_packet", {"year": "2026"})
    [church] = got["payers"]
    assert church["name"] == MINISTRY and church["form"] == "1099-NEC"
    assert church["came_in_seen"] == "86.40"
    assert church["paid_as_pay"] == [{"date": "2026-08-12", "dollars": "86.40"}]
    assert got["schedule_c"]["income"] == "86.40"
    assert f"{MINISTRY} paid $86.40 as pay, from its own books." in got["says"]


def test_their_own_word_on_the_ministry_as_a_payer_is_kept_and_counted_once(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "his pay", "pay_of": "Daniel Reyes"},
    )
    put = daniel.call(
        "keep_payers", {"payer": "Living Water", "kind": "sending church", "words": "LIVING WATER"}
    )
    assert put["status"] == "set", put
    got = daniel.call("tax_packet", {"year": "2026"})
    [church] = got["payers"]
    assert church["name"] == "Living Water" and church["came_in_seen"] == "86.40"


def test_pay_is_spending_in_the_books_and_the_export_ties(solo):  # noqa: F811
    """2026-11-15: a line named as someone's pay (c34 PR 2) reached the
    journal as Expenses:Pay but not the books' spending, so the accountant's
    export said it didn't tie ($1,375.26 in the journal, $1,288.86 in the
    books)."""
    from tests.unit.test_ministry_lines import _door

    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "his pay", "pay_of": "Daniel Reyes"},
    )
    linda = _door(solo, "linda-park")
    books = linda.call("books", {"period": "2026"})
    assert books["spent_by_kind"]["Pay"] == "86.40"
    assert books["spent"] == "1375.26"
    done = linda.call("cpa_export", {"year": "2026"})
    assert done["matches_the_books"], done["says"]


# Engine #653: at a solo ministry both accounts are his. The ministry's
# transfer to his own account was matched as a payback when he kept his own
# statement; when he then says it was his pay, it is his pay, and the deposit
# is that pay arriving. Either order reads the same.
OWN_PAY_CSV = (
    "Date,Description,Amount\n"
    "08/13/2026,TRANSFER FROM CHK 4417,86.40\n"
    "08/14/2026,TAXI KATHMANDU,-9.00\n"
)


def _keep_own_deposit(daniel, desk, token, solo) -> dict:  # noqa: F811
    from tests.unit.test_bank_balance import _held, _send

    _send(solo, desk, token, OWN_PAY_CSV.encode())
    held = _held(daniel)["2026-08-13"]
    daniel.call("bank_statement", {"statement": held["statement"], "account": "own"})
    kept = daniel.call(
        "keep_own_lines", {"statement": held["statement"], "rows": [1], "said": "the transfer"}
    )
    assert kept["status"] == "kept", kept
    return kept


def _name_pay(daniel) -> None:
    put = daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "his pay", "pay_of": "Daniel Reyes"},
    )
    assert put["status"] == "set", put


def _reads_as_pay(daniel) -> None:
    assert _row(daniel, "2026-08-12")["role"] == st.PAY_OUT
    assert _row(daniel, "2026-08-13")["role"] == st.PAY_IN
    got = daniel.call("tax_packet", {"year": "2026"})
    [church] = got["payers"]
    assert church["came_in_seen"] == "86.40"  # once, never $172.80
    assert church["deposits"] == [{"date": "2026-08-13", "dollars": "86.40"}]
    assert got["schedule_c"]["income"] == "86.40"


def test_pay_named_after_the_transfer_was_matched_is_still_pay(solo):  # noqa: F811
    daniel, desk, token, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    _keep_own_deposit(daniel, desk, token, solo)
    assert _row(daniel, "2026-08-12")["role"] == st.PAID_BACK  # before he says
    _name_pay(daniel)
    _reads_as_pay(daniel)


def test_pay_named_before_his_own_statement_is_never_called_paid_back(solo):  # noqa: F811
    daniel, desk, token, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    _name_pay(daniel)
    kept = _keep_own_deposit(daniel, desk, token, solo)
    assert "paid back" not in kept["say"]
    _reads_as_pay(daniel)
