"""The bank's own balance, kept from the ministry's statement (board item
t-books-page, part 1; the owner, 2026-11-11: "go ahead with balances").

A bank's CSV export carries a Balance column after each line. The ministry's
account keeps the balance at each day's end; the earliest one is where the
books start, and every later one checks them: the bank and the books agree,
or the gap is named with its days and its dollars (a statement never sent, a
line twice). The person's own account never keeps a balance: it is their
private life, and only the lines they pick are ever the ministry's."""

from __future__ import annotations

from core.agents.expenses import balances
from core.agents.expenses import statements as st
from tests.unit.test_bank_catch_up import _daniel, _events, _everything, _held
from tests.unit.test_catch_up import _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_statements import MINISTRY_CSV

CHASE_CSV = """Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
DEBIT,10/05/2026,"FOREIGN EXCH RT ADJ FEE 10/03",-3.00,FEE_TRANSACTION,7011.07,,
DEBIT,10/05/2026,"NON-CHASE ATM FEE-WITH",-5.00,FEE_TRANSACTION,7014.07,,
DEBIT,10/05/2026,"NON-CHASE ATM WITHDRAW 000903 10/03 POKHARA NP",-100.00,ATM,7019.07,,
CREDIT,10/01/2026,"ZELLE PAYMENT FROM M PORTER 25010234590",500.00,QUICKPAY_CREDIT,7119.07,,
DEBIT,09/28/2026,"REMITLY* SEND TO NEPAL",-400.00,ACH_DEBIT,6619.07,,
"""
"""Chase's own shape: newest first, so a day's last line is its first row."""

OCTOBER_CSV = """Date,Description,Debit,Credit,Balance
10/06/2026,ONLINE GIFT HOLLIS FAMILY,,100.00,"3,474.74"
10/09/2026,KATHMANDU GUEST HOUSE,74.74,,"3,400.00"
"""
"""October, after MINISTRY_CSV's August and September."""


def _day_ends(lines: list[st.Line]) -> dict[str, int]:
    return {ln.day: ln.day_end for ln in lines if ln.day_end is not None}


# ---- reading the column ------------------------------------------------------------


def test_a_statement_keeps_each_days_closing_balance():
    lines = st.parse(MINISTRY_CSV)
    assert _day_ends(lines) == {
        "2026-08-03": 525000,
        "2026-08-05": 475000,
        "2026-08-12": 466360,
        "2026-08-20": 345910,
        "2026-09-02": 337474,  # the second Staples line closes the day, not the first
    }
    staples = [ln for ln in lines if ln.day == "2026-09-02"]
    assert [ln.day_end for ln in staples] == [None, 337474]


def test_newest_first_reads_the_days_last_line_from_its_first_row():
    assert _day_ends(st.parse(CHASE_CSV)) == {
        "2026-09-28": 661907,
        "2026-10-01": 711907,
        "2026-10-05": 701107,  # after the fee on top, not the withdrawal below it
    }


def test_one_day_alone_reads_its_order_from_the_arithmetic():
    one_day = "\n".join(CHASE_CSV.splitlines()[:4]) + "\n"
    assert _day_ends(st.parse(one_day)) == {"2026-10-05": 701107}


def test_no_balance_column_keeps_no_balance_and_reads_the_same_lines():
    plain = st.parse(MINISTRY_CSV.replace(",Balance", "").replace(',"5,250.00"', ""))
    assert _day_ends(plain) == {}
    assert [ln.sha("d", "ministry") for ln in plain] == [
        ln.sha("d", "ministry") for ln in st.parse(MINISTRY_CSV)
    ]


def test_a_blank_balance_is_never_guessed():
    blank = MINISTRY_CSV.replace('42.18,,"3,374.74"', "42.18,,")
    assert "2026-09-02" not in _day_ends(st.parse(blank))


# ---- the bank against the books (pure) ----------------------------------------------


def _log(csv: str, person: str = "Daniel Reyes") -> list[dict]:
    """One kept ministry statement: its lines, and its balances when it had any."""
    lines = st.parse(csv)
    out = [
        {"event_type": st.LINE_EVENT, "payload": ln.payload(person, st.MINISTRY)} for ln in lines
    ]
    days = {ln.day: ln.day_end for ln in lines if ln.day_end is not None}
    if days:
        shas = [ln.sha(person, st.MINISTRY) for ln in lines]
        out.append(
            {"event_type": balances.BALANCE_EVENT, "payload": balances.payload(person, days, shas)}
        )
    return out


def test_the_books_tie_to_every_balance_the_bank_gave():
    [stand] = balances.standing(_log(MINISTRY_CSV), through="2026-09-30")
    assert stand.person == "Daniel Reyes"
    assert (stand.bank_on, stand.bank) == ("2026-09-02", 337474)
    assert stand.books == 337474 and stand.gaps == ()
    assert stand.opening == ("2026-08-03", 500000)  # the day before the first line


def test_the_balance_reads_as_of_any_day():
    [stand] = balances.standing(_log(MINISTRY_CSV), through="2026-08-31")
    assert (stand.bank_on, stand.bank, stand.books) == ("2026-08-20", 345910, 345910)


def test_a_line_never_sent_is_named_with_its_days_and_dollars():
    log = [
        e
        for e in _log(MINISTRY_CSV)
        if not (e["event_type"] == st.LINE_EVENT and e["payload"]["day"] == "2026-08-12")
    ]
    [stand] = balances.standing(log, through="2026-09-30")
    assert stand.gaps == (balances.Gap(after="2026-08-05", on="2026-08-12", cents=-8640),)
    assert stand.books == stand.bank  # one gap, named once; later days tie again


def test_a_statement_never_sent_shows_as_a_gap_between_two():
    log = _log(MINISTRY_CSV) + _log(OCTOBER_CSV)
    [stand] = balances.standing(log, through="2026-10-31")
    assert stand.gaps == () and stand.bank == 340000
    # October's first week never sent: only a later statement, from the 9th.
    late = (
        "Date,Description,Debit,Credit,Balance\n"
        '10/09/2026,KATHMANDU GUEST HOUSE,74.74,,"3,400.00"\n'
    )
    [stand] = balances.standing(_log(MINISTRY_CSV) + _log(late), through="2026-10-31")
    assert stand.gaps == (balances.Gap(after="2026-09-02", on="2026-10-09", cents=10000),)


def test_lines_after_the_last_balance_count_on_the_books():
    later = MINISTRY_CSV.replace(
        ",Ending balance", "09/20/2026,ONLINE GIFT,,25.00,\n,Ending balance"
    )
    [stand] = balances.standing(_log(later), through="2026-09-30")
    assert (stand.bank_on, stand.bank, stand.books) == ("2026-09-02", 337474, 339974)


def test_lines_from_a_statement_with_no_balance_never_read_as_a_gap():
    """The practice box, 2026-11-11: Ruth's earlier made-up account, sent
    before balances were kept, mixed in and read as eight gaps."""
    other = "Date,Description,Amount\n08/10/2026,ATM WITHDRAWAL,-200.00\n09/01/2026,GIFT,50.00\n"
    [stand] = balances.standing(_log(other) + _log(MINISTRY_CSV), through="2026-09-30")
    assert stand.gaps == () and stand.books == 337474


def test_no_balance_kept_means_nothing_to_say():
    plain = [e for e in _log(MINISTRY_CSV) if e["event_type"] == st.LINE_EVENT]
    assert balances.standing(plain, through="2026-09-30") == []


# ---- through the door -----------------------------------------------------------------


def _keep_ministry(solo, csv: str):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, csv.encode())
    [held] = door.call("bank_statement", {})["statements"]
    said = door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    return door, desk, token, said


def test_the_ministrys_statement_keeps_its_balances_once(solo):  # noqa: F811
    door, desk, token, said = _keep_ministry(solo, MINISTRY_CSV)
    assert said["status"] == "kept"
    assert "The bank says $3,374.74 at the end of Sep 2, 2026, and the books agree." in said["say"]
    [kept] = _events(solo, balances.BALANCE_EVENT)
    assert kept["account"] == "ministry" and len(kept["lines"]) == 6
    assert list(kept["days"].items())[-1] == ("2026-09-02", 337474) and len(kept["days"]) == 5
    _send(solo, desk, token, MINISTRY_CSV.encode())
    [again] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": again["statement"], "account": "ministry"})
    assert len(_events(solo, balances.BALANCE_EVENT)) == 1


def test_sending_a_statement_again_adds_the_balances_its_lines_lacked(solo):  # noqa: F811
    plain = MINISTRY_CSV.replace(",Balance", "")
    door, desk, token, _said = _keep_ministry(solo, plain)
    assert _events(solo, balances.BALANCE_EVENT) == []
    lines = len(_events(solo, st.LINE_EVENT))
    _send(solo, desk, token, MINISTRY_CSV.encode())
    [again] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": again["statement"], "account": "ministry"})
    assert len(_events(solo, st.LINE_EVENT)) == lines  # the same lines, never twice
    assert len(_events(solo, balances.BALANCE_EVENT)) == 1


def test_their_own_accounts_balance_is_never_kept(solo):  # noqa: F811
    own = "Date,Description,Amount,Balance\n08/07/2026,TAXI KATHMANDU,-6.80,9876.54\n"
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, own.encode())
    held = _held(door)["2026-08-07"]
    walked = door.call("bank_statement", {"statement": held["statement"], "account": "own"})
    assert all("9876" not in str(v) for r in walked["rows"] for v in r.values())
    door.call("keep_own_lines", {"statement": held["statement"], "rows": [1], "said": "mine"})
    assert _events(solo, balances.BALANCE_EVENT) == []
    assert "9876.54" not in _everything(solo) and "987654" not in _everything(solo)


def test_the_books_say_whats_in_the_bank(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    q3 = door.call("books", {"period": "2026-Q3"})
    assert q3["in_the_bank"] == {
        "balance": "3374.74",
        "bank_said": "3374.74",
        "bank_said_on": "2026-09-02",
        "opening": "5000.00",
        "opening_on": "2026-08-03",
        "agrees": True,
    }
    assert "$3,374.74 in the bank" in q3["says"]
    august = door.call("books", {"period": "August 2026"})
    assert august["in_the_bank"]["balance"] == "3459.10"
    july = door.call("books", {"period": "July 2026"})
    assert "in_the_bank" not in july  # nothing the bank said by then


def test_the_books_name_a_gap_in_plain_words(solo):  # noqa: F811
    missing = MINISTRY_CSV.replace(
        '08/12/2026,ONLINE TRANSFER TO CHK ...9921,86.40,,"4,663.60"\n', ""
    )
    assert missing != MINISTRY_CSV
    door, *_rest = _keep_ministry(solo, missing)
    q3 = door.call("books", {"period": "2026-Q3"})
    bank = q3["in_the_bank"]
    assert bank["agrees"] is False
    assert bank["gaps"] == [
        {"after": "2026-08-05", "on": "2026-08-20", "bank_less_books": "-86.40"}
    ]
    assert "the bank has $86.40 less than the books" in bank["say"]
