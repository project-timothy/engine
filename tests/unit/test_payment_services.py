"""Gifts through a payment service (board chunk c32, the PayPal half; the
owner, 2026-11-12: PayPal first, built as one import for every service a
ministry uses, so a church's Planning Center Giving goes through the same
code).

A service's export is its own money account in the books, never lines on
the bank's account. A completed gift is income at its gross with its donor
named, the service's fee is spending, the net is what the service holds. A
payout and the bank deposit it became are money moving between the
ministry's own accounts: never income, counted once at the service."""

from __future__ import annotations

import pytest

from core.agents.expenses import journal, processors
from core.agents.expenses import statements as st
from tests.unit.test_bank_catch_up import _daniel, _events
from tests.unit.test_catch_up import _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door

PAYPAL_CSV = (
    '"Date","Time","TimeZone","Name","Type","Status","Currency","Gross","Fee","Net",'
    '"From Email Address","To Email Address","Transaction ID","Balance"\n'
    '"08/02/2026","09:14:02","EDT","Hollis Family","Donation Payment","Completed","USD",'
    '"300.00","-10.96","289.04","hollis@example.org","give@example.org","1AB11111","289.04"\n'
    '"08/03/2026","10:01:00","EDT","Grace Okoro","Donation Payment","Completed","USD",'
    '"50.00","-2.24","47.76","grace@example.org","give@example.org","2CD22222","336.80"\n'
    '"08/04/2026","11:00:00","EDT","Sam Lee","Donation Payment","Pending","USD",'
    '"25.00","-1.37","23.63","sam@example.org","give@example.org","3EF33333","336.80"\n'
    '"08/04/2026","11:30:00","EDT","PayPal Giving Fund","Donation Payment","Completed","USD",'
    '"120.00","0.00","120.00","","give@example.org","4GH44444","456.80"\n'
    '"08/05/2026","12:00:00","EDT","Hollis Family","Payment Refund","Completed","USD",'
    '"-50.00","1.60","-48.40","","hollis@example.org","6KL66666","408.40"\n'
    '"08/06/2026","08:00:00","EDT","","General Withdrawal","Completed","USD",'
    '"-408.40","0.00","-408.40","","","5IJ55555","0.00"\n'
)
"""PayPal's activity download, as its own headers name the columns."""

BANK_CSV = """Living Water Ministry Checking ...4417
Date,Description,Debit,Credit,Balance
08/03/2026,ONLINE GIFT HOLLIS FAMILY,,250.00,"5,250.00"
08/07/2026,PAYPAL TRANSFER 1000245,,408.40,"5,658.40"
08/20/2026,KATHMANDU GUEST HOUSE,"1,204.50",,"4,453.90"
"""
"""The ministry's bank: the PayPal payout lands a day later."""

PLANNING_CENTER_CSV = """Donation ID,Received Date,First Name,Last Name,Fund,Payment Method,\
Amount,Fee,Net,Status
pc-1001,2026-08-09,Ruth,Okafor,General,card,100.00,3.20,96.80,succeeded
pc-1002,2026-08-10,Tom,Baker,Missions,ach,500.00,4.00,496.00,succeeded
pc-1003,2026-08-11,Ann,Lee,General,card,20.00,0.88,19.12,failed
"""
"""A church giving app's export, made up in the shape such exports take
(a fee charged on top, first and last names apart, a fund per gift)."""


# ---- reading an export --------------------------------------------------------------


def test_paypals_export_reads_as_gifts_refunds_and_a_payout():
    got = processors.parse(PAYPAL_CSV)
    assert got.service == "PayPal" and got.gifts == 3
    assert [(p.day, p.kind, p.gross, p.net, p.name) for p in got.payments] == [
        ("2026-08-02", processors.GIFT, 30000, 28904, "Hollis Family"),
        ("2026-08-03", processors.GIFT, 5000, 4776, "Grace Okoro"),
        ("2026-08-04", processors.GIFT, 12000, 12000, "PayPal Giving Fund"),
        ("2026-08-05", processors.REFUND, -5000, -4840, "Hollis Family"),
        ("2026-08-06", processors.PAYOUT, -40840, -40840, ""),
    ]
    assert got.payments[0].fee == 1096 and got.payments[3].fee == -160  # given back
    assert got.left_out == ("Donation Payment",)  # Sam Lee's, still pending


def test_a_church_giving_apps_export_goes_through_the_same_code():
    got = processors.parse(PLANNING_CENTER_CSV)
    assert got.service == ""  # Tim asks which service it is
    ruth, tom = got.payments
    assert (ruth.name, ruth.gross, ruth.fee, ruth.net, ruth.fund) == (
        "Ruth Okafor",
        10000,
        320,
        9680,
        "General",
    )
    assert (tom.name, tom.txn, tom.fund) == ("Tom Baker", "pc-1002", "Missions")
    assert got.left_out == ("failed",)


def test_a_bank_statement_is_never_an_export_and_an_export_never_a_statement():
    assert not processors.is_export(BANK_CSV)
    assert processors.is_export(PAYPAL_CSV) and processors.is_export(PLANNING_CENTER_CSV)
    with pytest.raises(st.NotAStatement):
        st.parse(PAYPAL_CSV)
    with pytest.raises(processors.NotAnExport):
        processors.parse(BANK_CSV)


def test_a_fee_that_doesnt_square_with_the_net_refuses_the_file():
    bad = PLANNING_CENTER_CSV.replace("100.00,3.20,96.80", "100.00,3.20,95.80")
    with pytest.raises(processors.NotAnExport) as caught:
        processors.parse(bad)
    assert "gross less its fee isn't its net" in caught.value.say


def test_the_same_export_twice_is_the_same_rows():
    one = {p.sha("Daniel Reyes", "PayPal") for p in processors.parse(PAYPAL_CSV).payments}
    two = {p.sha("Daniel Reyes", "PayPal") for p in processors.parse(PAYPAL_CSV).payments}
    assert one == two and len(one) == 5


# ---- through the door ---------------------------------------------------------------


def _held(door) -> list[dict]:
    return door.call("bank_statement", {})["statements"]


def _send_paypal(solo, csv: str = PAYPAL_CSV):  # noqa: F811
    door, desk, token = _daniel(solo)
    said = _send(solo, desk, token, csv.encode())
    return door, desk, token, said


def _keep(door, kind: str, **args) -> dict:
    [held] = [h for h in _held(door) if h["kind"] == kind]
    return door.call("bank_statement", {"statement": held["statement"], **args})


def _bank(door, month: str = "2026-08") -> list[dict]:
    [me] = door.call("month_report", {"month": month})["people"]
    return me["bank"]


def _row(door, words: str) -> dict:
    return next(b for b in _bank(door) if b["words"] == words)


def test_an_export_through_the_catch_up_link_is_held_as_a_payment_service(solo):  # noqa: F811
    door, _desk, _token, said = _send_paypal(solo)
    assert said[1]["say"] == (
        "Got your PayPal export: 3 gifts, 1 refund, and 1 payout, Aug 2 to Aug 6. Tell Tim "
        "whether it's the ministry's own PayPal account."
    )
    [held] = _held(door)
    assert held["kind"] == "payment service export" and held["service"] == "PayPal"


def test_the_ministrys_paypal_keeps_each_gift_with_its_donor_and_its_fee(solo):  # noqa: F811
    door, *_rest = _send_paypal(solo)
    kept = _keep(door, "payment service export", account="ministry")
    assert kept["status"] == "kept", kept
    assert kept["say"].startswith(
        "Kept PayPal's 3 gifts ($470.00), 1 refund ($50.00), and 1 payout to the bank. "
        "Left out: Donation Payment."
    )
    hollis = _row(door, "Gift through PayPal: Hollis Family")
    assert (hollis["role"], hollis["dollars"], hollis["gift_from"]) == (
        st.CAME_IN,
        "300.00",
        "Hollis Family",
    )
    fee = _row(door, "PayPal's fee: Hollis Family")
    assert (fee["role"], fee["dollars"], fee["category"]) == (st.MINISTRY_PAID, "10.96", "Other")
    assert fee["waiting_on"] is None and fee["paid_from"] == st.PROCESSOR
    assert _row(door, "Gift through PayPal: PayPal Giving Fund")["fund_grant"] is True
    refund = _row(door, "Refund through PayPal: Hollis Family")
    assert (refund["role"], refund["dollars"]) == (st.CAME_IN, "-50.00")
    payout = _row(door, "PayPal to the bank")
    assert payout["role"] == st.MOVED and payout["dollars"] == "408.40"
    assert payout["waiting_on"] == processors.WAITING_DEPOSIT
    assert "names" not in hollis  # a gift through a service never asks whether it was pay
    assert len(_events(solo, processors.PAYMENT_EVENT)) == 5


def test_sending_the_export_again_keeps_nothing_twice(solo):  # noqa: F811
    door, desk, token, _said = _send_paypal(solo)
    _keep(door, "payment service export", account="ministry")
    _send(solo, desk, token, PAYPAL_CSV.encode())
    again = _keep(door, "payment service export", account="ministry")
    assert again["status"] == "kept"
    assert len(_events(solo, processors.PAYMENT_EVENT)) == 5


def test_a_persons_own_paypal_is_never_kept(solo):  # noqa: F811
    door, *_rest = _send_paypal(solo)
    said = _keep(door, "payment service export", account="own")
    assert said["status"] == "refused"
    assert _events(solo, processors.PAYMENT_EVENT) == []
    assert _keep(door, "payment service export")["status"] == "which"


def test_a_service_tim_cant_name_is_asked_and_kept_under_the_name_they_give(solo):  # noqa: F811
    door, *_rest = _send_paypal(solo, PLANNING_CENTER_CSV)
    asks = _keep(door, "payment service export", account="ministry")
    assert asks["status"] == "which" and "which service" in asks["say"]
    kept = _keep(door, "payment service export", account="ministry", service="Planning Center")
    assert kept["status"] == "kept", kept
    ruth = _row(door, "Gift through Planning Center: Ruth Okafor")
    assert ruth["gift_from"] == "Ruth Okafor" and ruth["dollars"] == "100.00"


def _both(solo):  # noqa: F811
    """The PayPal export and the bank statement, kept in that order."""
    door, desk, token, _said = _send_paypal(solo)
    _keep(door, "payment service export", account="ministry")
    _send(solo, desk, token, BANK_CSV.encode())
    kept = _keep(door, "bank statement", account="ministry")
    assert kept["status"] == "kept", kept
    return door


def test_the_payout_and_its_bank_deposit_are_one_move_never_income(solo):  # noqa: F811
    door = _both(solo)
    deposit = _row(door, "PAYPAL TRANSFER 1000245")
    assert deposit["role"] == st.MOVED and deposit["processor"] == "PayPal"
    assert _row(door, "PayPal to the bank")["waiting_on"] is None
    # Gross receipts: the bank's own gift and PayPal's gifts less the refund, once.
    log = [
        {"event_type": k, "payload": p}
        for k in (st.LINE_EVENT, processors.PAYMENT_EVENT)
        for p in _events(solo, k)
    ]
    assert st.came_in(log, "2026-08-01", "2026-08-31") == 25000 + 42000


def test_the_books_tie_to_the_bank_with_paypal_in_them(solo):  # noqa: F811
    door = _both(solo)
    books = door.call("books", {"period": "2026-08"})
    assert books["came_in"] == "670.00"
    assert books["spent_by_kind"]["Other"] == "11.60"  # PayPal's fees, less the one given back
    d = books["double_entry"]
    assert d["balanced"] and d["ties_to_the_bank"]
    assert d["accounts_at_end"]["Assets:Bank:Daniel Reyes"] == "4453.90"
    assert f"{processors.ROOT}:PayPal:Daniel Reyes" not in d["accounts_at_end"]  # all paid out


def test_paypal_held_at_month_end_is_its_own_account_and_the_pack_ties(solo):  # noqa: F811
    door, *_rest = _send_paypal(solo)
    _keep(door, "payment service export", account="ministry")
    linda = _door(solo, "linda-park")
    books = linda.call("books", {"period": "2026-08"})
    # No bank statement yet: the payout waits on its deposit, PayPal still holds it.
    assert (
        books["double_entry"]["accounts_at_end"][f"{processors.ROOT}:PayPal:Daniel Reyes"]
        == "408.40"
    )
    pack = linda.call("board_pack", {"period": "2026-08"})
    assert pack["balanced"] and pack["matches_the_books"]
    assert pack["position"]["with_payment_services"] == {"PayPal (Daniel Reyes)": "408.40"}


def test_paypal_donors_get_their_letters(solo):  # noqa: F811
    _both(solo)
    got = _door(solo, "linda-park").call("donor_letters", {"year": "2026"})
    grace, hollis = got["donors"]
    assert grace["name"] == "Grace Okoro" and grace["total"] == "50.00"
    assert hollis["total"] == "250.00"  # $300.00 less the $50.00 refunded
    assert hollis["letters"][0] == "an acknowledgment of the Aug 2, 2026 gift of $300.00"
    [grant] = got["fund_grants"]
    assert grant["from"] == "PayPal Giving Fund" and grant["dollars"] == "120.00"


def test_the_journal_names_the_services_account():
    row = {
        "date": "2026-08-02",
        "words": "Gift through PayPal: Hollis Family",
        "dollars": "300.00",
        "role": st.CAME_IN,
        "category": None,
        "paid_from": st.PROCESSOR,
        "processor": "PayPal",
    }
    fee = row | {"dollars": "10.96", "role": st.MINISTRY_PAID, "category": "Other"}
    made, waiting = journal.month_entries([{"person": "Ruth", "bank": [row, fee]}])
    assert waiting == 0
    assert journal.totals(made) == {
        "Assets:Processor:PayPal:Ruth": 28904,
        "Expenses:Other": 1096,
        "Income:Uncategorized": -30000,
    }


def test_a_services_fees_never_go_on_the_reviewers_card(solo):  # noqa: F811
    import json
    import sqlite3
    from pathlib import Path

    from core.engine.runner import run

    door, *_rest = _send_paypal(solo)
    _keep(door, "payment service export", account="ministry")
    done = run(
        "living-water",
        "expenses",
        "month",
        params={"month": "2026-08"},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )
    assert done.status != "error", done.summary
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    cards = [json.loads(p) for (p,) in con.execute("SELECT params_json FROM approval_queue")]
    con.close()
    assert not any("PayPal" in str(c.get("lines", "")) for c in cards)
