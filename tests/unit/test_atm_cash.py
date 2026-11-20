"""Cash from an ATM (board t-atm-cash, the owner's decision of 2026-10-15).

A missionary with no remittance service spends from the ministry's own
account and draws rupees at an ATM. The withdrawal is money moving (dollars
into rupees in hand), never spending; the rupees it gave come from the slip
or the person's word, and set what each rupee cost. Rupee receipts and rupee
school payments spend that cash down, oldest cash first, at its cost, never
at the day's rate; the cash came from the ministry, so nothing is owed back.
The bank's ATM fee is its own line. What's left is cash on hand.

Made-up numbers: $300 gave NPR 40,000 on Aug 3 (133.33 to the dollar) and
$150 gave NPR 21,000 on Aug 20 (140 to the dollar). A NPR 4,300 receipt on
Aug 7 costs $32.25; a NPR 12,000 scholarship on Aug 18 costs $90.00; a
NPR 30,000 receipt on Aug 25 takes the last 23,700 of the first cash
($177.75) and 6,300 of the second ($45.00): $222.75. NPR 14,700 is left,
which cost $105.00.
"""

from __future__ import annotations

from decimal import Decimal

from core.agents.expenses import records
from core.agents.expenses import statements as st
from tests.unit.test_bank_catch_up import _daniel, _send
from tests.unit.test_catch_up import NOW
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_missionary_month import _cards, _month, feed  # noqa: F401
from tests.unit.test_statements import SCHOOL_CSV

ATM_CSV = """Date,Description,Debit,Credit
08/03/2026,ATM WITHDRAWAL 000123 KATHMANDU NP,300.00,
08/03/2026,NON-CHASE ATM FEE-WITH,5.00,
08/03/2026,FOREIGN EXCH RT ADJ FEE,9.00,
08/20/2026,ATM WITHDRAWAL 000456 POKHARA NP,150.00,
"""

PERSON = "Daniel Reyes"


# ---- the lines ----------------------------------------------------------------------


def test_an_atm_withdrawal_is_cash_and_its_fee_is_a_bank_fee():
    atm, fee, fx_fee, _second = st.parse(ATM_CSV)
    assert atm.kind == st.ATM and not atm.bank_fee
    assert fee.kind == st.OUT and fee.bank_fee
    assert fx_fee.kind == st.OUT and fx_fee.bank_fee
    assert st.looks_like(atm, []) == "cash drawn"
    shop = st.Line("2026-08-03", "SCHOOL FEE PAYMENT", -5000)
    assert not shop.bank_fee  # a fee the ministry paid someone still says what it was for


CHASE_CSV = """Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
DEBIT,10/03/2026,"NON-CHASE ATM WITHDRAW 000123 10/03 KATHMANDU NP",-300.00,ATM,4700.00,,
DEBIT,10/03/2026,"NON-CHASE ATM FEE-WITH",-5.00,FEE_TRANSACTION,4695.00,,
DEBIT,10/03/2026,"FOREIGN EXCH RT ADJ FEE 10/03",-9.00,FEE_TRANSACTION,4686.00,,
DEBIT,10/05/2026,"BHATBHATENI SUPERMARKE KATHMANDU NP",-31.62,DEBIT_CARD,4654.38,,
"""


def test_a_chase_export_reads_its_atm_cash_and_fees():
    """The alpha missionary banks at Chase: its export's Details column is
    DEBIT or CREDIT, so the bank's words come from Description."""
    atm, fee, fx_fee, shop = st.parse(CHASE_CSV)
    assert (atm.kind, atm.cents) == (st.ATM, -30000) and atm.words.startswith("NON-CHASE ATM")
    assert fee.bank_fee and fx_fee.bank_fee
    assert shop.kind == st.OUT and not shop.bank_fee


# ---- the cash, by code ------------------------------------------------------------------


def _line(day: str, words: str, cents: int, n: int = 0) -> dict:
    return {
        "event_type": st.LINE_EVENT,
        "payload": st.Line(day, words, cents, n).payload(PERSON, st.MINISTRY),
    }


def _gave(line: dict, tag: str) -> dict:
    sha = line["payload"]["sha256"]
    return {"event_type": st.PURPOSE_EVENT, "payload": {"sha256": sha, "said": tag, "cash": tag}}


def _receipt(sha: str, day: str, tag: str) -> dict:
    name = f"{day}_receipt_{tag}.jpg"
    payload = {"sha256": sha, "file": name, "person": PERSON}
    return {"event_type": records.LANDED_EVENT, "payload": payload}


def _school(day: str, amount: str, currency: str) -> dict:
    row = st.SchoolPayment(day, "Asha Gurung", "Shree Janata School", Decimal(amount), currency)
    return {"event_type": st.SCHOOL_EVENT, "payload": row.payload(PERSON)}


def _log() -> list[dict]:
    first = _line("2026-08-03", "ATM WITHDRAWAL 000123 KATHMANDU NP", -30000)
    second = _line("2026-08-20", "ATM WITHDRAWAL 000456 POKHARA NP", -15000)
    return [
        first,
        second,
        _gave(first, "NPR40000.00"),
        _gave(second, "NPR21000.00"),
        _receipt("r1" * 32, "2026-08-07", "NPR4300.00"),
        _school("2026-08-18", "12000", "NPR"),
        _receipt("r2" * 32, "2026-08-25", "NPR30000.00"),
    ]


def test_receipts_spend_the_oldest_cash_first_at_what_it_cost():
    drawn, on_hand = st.cash(_log())
    school = next(e["payload"]["sha256"] for e in _log() if e["event_type"] == st.SCHOOL_EVENT)
    assert drawn["r1" * 32].cents == 3225 and drawn["r1" * 32].atm_day == "2026-08-03"
    assert drawn[school].cents == 9000
    assert drawn["r2" * 32].cents == 22275  # 23,700 of the first cash, 6,300 of the second
    assert all(d.ministry for d in drawn.values())
    assert on_hand == {(PERSON, "NPR"): (Decimal("14700.00"), 10500)}


def test_cash_on_hand_at_a_months_end_counts_only_what_was_drawn_and_spent_by_then():
    _drawn, on_hand = st.cash(_log(), through="2026-08-10")
    assert on_hand == {(PERSON, "NPR"): (Decimal("35700.00"), 26775)}


def test_cash_whose_rupees_nobody_said_prices_nothing():
    log = [e for e in _log() if e["event_type"] != st.PURPOSE_EVENT]
    assert st.cash(log) == ({}, {})


def test_rupees_spent_before_any_cash_was_drawn_keep_the_days_rate():
    log = [*_log(), _receipt("r0" * 32, "2026-07-25", "NPR1000.00")]
    drawn, _on_hand = st.cash(log)
    assert "r0" * 32 not in drawn


def test_cash_drawn_on_a_weekend_pays_for_what_it_bought_before_the_bank_posted_it():
    """Board t-atm-cash, found 2026-10-15: a Saturday-night withdrawal in
    Kathmandu posts at Chase on Monday, and the cash bought Sunday's
    groceries. Chase's words carry the day it was drawn; without it, the
    receipt read as the person's own money (owed back) while the same
    rupees still sat in hand: twice the money."""
    atm = _line("2026-10-05", "NON-CHASE ATM WITHDRAW 000123 10/03 KATHMANDU NP", -30000)
    log = [atm, _gave(atm, "NPR40000.00"), _receipt("r1" * 32, "2026-10-04", "NPR4300.00")]
    drawn, on_hand = st.cash(log)
    assert drawn["r1" * 32].cents == 3225 and drawn["r1" * 32].atm_day == "2026-10-03"
    assert on_hand == {(PERSON, "NPR"): (Decimal("35700.00"), 26775)}
    _drawn, september = st.cash(log, through="2026-09-30")
    assert september == {}


def test_a_bank_that_gives_no_day_in_its_words_gets_a_few_days_of_grace():
    atm = _line("2026-10-05", "ATM WITHDRAWAL 000123 KATHMANDU NP", -30000)
    log = [
        atm,
        _gave(atm, "NPR40000.00"),
        _receipt("r1" * 32, "2026-10-03", "NPR4300.00"),
        _receipt("r2" * 32, "2026-09-30", "NPR1000.00"),
    ]
    drawn, _on_hand = st.cash(log)
    assert "r1" * 32 in drawn and "r2" * 32 not in drawn


def test_the_day_in_the_words_crosses_the_new_year_and_ignores_far_dates():
    assert st.Line("2027-01-02", "NON-CHASE ATM WITHDRAW 12/31 POKHARA", -100).drawn_on == (
        "2026-12-31"
    )
    assert st.Line("2026-10-05", "ATM 06/01 KATHMANDU", -100).drawn_on is None
    assert st.Line("2026-10-05", "ATM 10/03/2026 KATHMANDU", -100).drawn_on is None


def test_an_export_with_both_days_reads_the_transaction_date():
    both = (
        "Transaction Date,Posted Date,Description,Amount\n10/03/2026,10/05/2026,ATM KTM,-300.00\n"
    )
    [line] = st.parse(both)
    assert line.day == "2026-10-03"
    both = "Posting Date,Transaction Date,Description,Amount\n10/05/2026,10/03/2026,ATM,-300.00\n"
    [line] = st.parse(both)
    assert line.day == "2026-10-03"


# ---- through the door -------------------------------------------------------------------


def _receipt_on(door, desk, token, solo, day: str, said: str, photo: bytes):  # noqa: F811
    _send(solo, desk, token, photo, at=NOW)
    [line] = door.call("month_report", {"month": NOW.strftime("%Y-%m")})["people"][0]["lines"]
    door.call("set_receipt_day", {"line": line["source"], "said": day, "day": day})
    month = door.call("month_report", {"month": day[:7]})["people"][0]
    line = next(ln for ln in month["lines"] if ln["date"] == day and ln["dollars"] is None)
    door.call("set_amount", {"line": line["source"], "said": said})


def test_a_month_of_atm_cash_owes_nothing_and_says_what_is_left(solo, feed):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, ATM_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    bank = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    first, fee, fx_fee, second = sorted(bank, key=lambda b: (b["date"], -float(b["dollars"])))
    assert first["role"] == "cash drawn" and first["waiting_on"].startswith("how many rupees")
    assert fee["waiting_on"] is None and fee["category"] == "Other"
    assert fx_fee["waiting_on"] is None

    # No rupees in the words: Tim asks for the slip, and nothing is written.
    asked = door.call("explain_bank_line", {"line": first["source"], "said": "cash for the week"})
    assert asked["status"] == "asks" and "slip" in asked["say"]
    said = door.call(
        "explain_bank_line", {"line": first["source"], "said": "it gave 40,000 rupees"}
    )
    assert said["status"] == "set" and "133.33 to the dollar" in said["say"], said
    door.call("explain_bank_line", {"line": second["source"], "said": "21,000 rupees"})

    _receipt_on(door, desk, token, solo, "2026-08-07", "4,300 rupees", JPEG + b"a")
    _receipt_on(door, desk, token, solo, "2026-08-25", "30,000 rupees", JPEG + b"b")
    _send(solo, desk, token, SCHOOL_CSV.encode())
    school = next(s for s in door.call("bank_statement", {})["statements"])
    door.call("keep_school_list", {"statement": school["statement"], "said": "add them"})

    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    cash = next(b for b in month["bank"] if b["words"].startswith("ATM WITHDRAWAL 000123"))
    assert cash["gave"] == "NPR 40,000.00" and cash["waiting_on"] is None
    receipts = sorted(month["lines"], key=lambda ln: ln["date"])
    assert [ln["dollars"] for ln in receipts] == ["32.25", "222.75"]
    assert all(ln["paid"] == "from the ministry's cash" for ln in receipts)
    assert receipts[0]["rate_from"] == "ATM cash drawn 2026-08-03"
    asha = next(b for b in month["bank"] if b["words"].startswith("Asha"))
    assert asha["dollars"] == "90.00" and asha["role"] == "the ministry paid"
    bikash = next(b for b in month["bank"] if b["words"].startswith("Bikash"))
    # Paid in dollars from no cash: the ministry's money unless the list says
    # he paid it himself (board w-school-cash, 2026-10-16), so nothing is owed back.
    assert bikash["role"] == "the ministry paid"
    assert month["total"] == "0.00"
    assert month["cash_on_hand"] == "NPR 14,700.00 at the end of August (cost $105.00)"

    _month(solo, "2026-08")
    sent = door.call("send_month_for_review", {"month": "2026-08"})
    assert sent["status"] == "sent", sent
    [card] = [c for c in _cards(solo, "2026-08") if "lines" in c]
    assert card["total_cents"] == "0"
    assert "cash drawn" in card["moved"]
    # Each receipt the ministry's cash paid says so on the reviewer's card.
    paid = [ln for ln in card["lines"].split("; ") if "NPR" in ln and "the ministry paid" not in ln]
    assert paid and all("from the ministry's cash" in ln for ln in paid), card["lines"]


def test_cash_in_hand_says_when():
    """The dress rehearsal (2026-10-15): September's walk said the books held
    NPR 21,650 right after the count said NPR 26,700. Both were true; only
    one said when."""
    from datetime import date

    from core.tools.catalog import cash_when

    assert cash_when("2026-09", date(2026, 10, 16)) == "at the end of September"
    assert cash_when("2026-10", date(2026, 10, 16)) == "today"


def test_the_slips_number_is_the_local_money_and_a_date_is_never_the_amount(solo):  # noqa: F811
    """The dress rehearsal (2026-10-15): asked what the slips said, the
    missionary listed them ("Feb 14 gave 26,500, Apr 11 gave 19,900") with
    "rupees" said once, earlier. Every one was refused. An ATM in Kathmandu
    gives rupees; the date in front is never the amount; two numbers left
    are still asked about."""
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, ATM_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    bank = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    first, *_rest, second = sorted(
        (b for b in bank if b["role"] == "cash drawn"), key=lambda b: b["date"]
    )
    two = door.call(
        "explain_bank_line", {"line": first["source"], "said": "Aug 3 gave 40,000 or 41,000"}
    )
    assert two["status"] == "asks", two
    said = door.call("explain_bank_line", {"line": first["source"], "said": "Aug 3 gave 40,000"})
    assert said["status"] == "set" and "NPR 40,000.00" in said["say"], said
    slash = door.call("explain_bank_line", {"line": second["source"], "said": "8/20: 21,000"})
    assert slash["status"] == "set" and "NPR 21,000.00" in slash["say"], slash
