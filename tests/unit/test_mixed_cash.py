"""An envelope holding the ministry's cash and the person's own (the owner's
decision of 2026-10-16, board w-mixed-cash): the ministry's cash pays first,
the person's own after, and a purchase that takes both owes back only the
person's part. A school payment is the ministry's money unless the school
list says the person paid it themselves (board w-school-cash).

Made-up numbers: their own $80 gave NPR 10,000 on Aug 1 (0.8 cents a rupee);
the ministry's $30 gave NPR 4,000 on Aug 3 (0.75 cents a rupee). NPR 6,000
on Aug 7 takes the ministry's 4,000 ($30.00) and 2,000 of their own
($16.00): $46.00, of which $16.00 is owed back.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from core.agents.expenses import fx
from core.agents.expenses import statements as st
from core.tools.catalog import _paid_from
from tests.unit.test_atm_cash import PERSON, _gave, _receipt, _receipt_on
from tests.unit.test_bank_catch_up import _daniel, _held, _send
from tests.unit.test_cash_count import _split, _used
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_missionary_month import _cards, _month, feed  # noqa: F401

MIXED = "r6" * 32


def _atm(day: str, cents: int, tag: str, account: str) -> list[dict]:
    line = {
        "event_type": st.LINE_EVENT,
        "payload": st.Line(day, f"ATM WITHDRAWAL {day} KATHMANDU NP", -cents).payload(
            PERSON, account
        ),
    }
    return [line, _gave(line, tag)]


def _envelope() -> list[dict]:
    return [
        *_atm("2026-08-01", 8000, "NPR10000.00", st.OWN),
        *_atm("2026-08-03", 3000, "NPR4000.00", st.MINISTRY),
    ]


def _school(amount: str, currency: str, paid_by: str = "") -> dict:
    row = st.SchoolPayment(
        "2026-08-07", "Asha Gurung", "Shree Janata School", Decimal(amount), currency, paid_by
    )
    return {"event_type": st.SCHOOL_EVENT, "payload": row.payload(PERSON)}


# ---- whose cash pays ------------------------------------------------------------------


def test_the_ministrys_cash_pays_first_even_when_their_own_is_older():
    log = [*_envelope(), _receipt(MIXED, "2026-08-07", "NPR3000.00")]
    drawn, on_hand = st.cash(log)
    assert drawn[MIXED] == fx.Drawn(2250, "133.3333", "2026-08-03", True)
    assert drawn[MIXED].owed() == 0
    assert on_hand[(PERSON, "NPR")][0] == Decimal("11000.00")


def test_a_purchase_from_both_owes_back_only_their_part():
    log = [*_envelope(), _receipt(MIXED, "2026-08-07", "NPR6000.00")]
    d = st.cash(log)[0][MIXED]
    assert (d.cents, d.ministry, d.own_cents, d.owed()) == (4600, True, 1600, 1600)


def test_their_own_cash_alone_is_owed_back_whole():
    log = [
        *_atm("2026-08-01", 8000, "NPR10000.00", st.OWN),
        _receipt(MIXED, "2026-08-07", "NPR6000.00"),
    ]
    d = st.cash(log)[0][MIXED]
    assert (d.ministry, d.owed()) == (False, 4800)


def test_the_receipt_says_whose_cash_paid_it():
    log = [*_envelope(), _receipt(MIXED, "2026-08-07", "NPR6000.00")]
    paid = _paid_from(st.cash(log)[0][MIXED])
    assert paid == {"paid": "from the ministry's cash and $16.00 of your own", "owed_back": "16.00"}
    assert _paid_from(fx.Drawn(2250, "133.3333", "2026-08-03", True)) == {
        "paid": "from the ministry's cash"
    }


# ---- the rows a month reads ----------------------------------------------------------


def test_a_school_payment_from_both_is_the_ministrys_with_their_part_owed():
    log = [*_envelope(), _school("6000", "NPR")]
    [row] = [ln for ln in st.month_lines(log, "2026-08") if ln.get("school")]
    assert (row["role"], row["cents"], row["owed_cents"]) == (st.MINISTRY_PAID, 4600, 1600)


def test_cash_with_no_receipt_from_both_owes_back_their_part():
    log = [*_envelope(), _used("u1", "2026-08-07", "NPR6000.00", "ministry", "tea, taxis")]
    [row] = [ln for ln in st.month_lines(log, "2026-08") if ln.get("cash_used")]
    assert (row["role"], row["cents"], row["owed_cents"]) == (st.MINISTRY_PAID, 4600, 1600)


def test_cash_from_both_on_their_own_use_owes_the_ministry_only_its_part():
    log = [*_envelope(), _used("u1", "2026-08-07", "NPR6000.00", "own", "family")]
    [repay] = [ln for ln in st.month_lines(log, "2026-08") if ln["role"] == st.REPAY]
    assert repay["cents"] == 3000


def test_their_part_of_a_split_from_both_owes_the_ministry_only_its_part():
    log = [*_envelope(), _receipt(MIXED, "2026-08-07", "NPR1000.00"), _split(MIXED, "NPR5000.00")]
    [repay] = [ln for ln in st.month_lines(log, "2026-08") if ln["role"] == st.REPAY]
    # The receipt took NPR 1,000 of the ministry's cash; their NPR 5,000 took the
    # ministry's last 3,000 ($22.50, owed to it) and 2,000 of their own.
    assert repay["cents"] == 2250


# ---- school payments (w-school-cash) -------------------------------------------------


def test_a_school_payment_from_no_cash_is_the_ministrys_money():
    [row] = st.month_lines([_school("85.00", "USD")], "2026-08")
    assert (row["role"], row["cents"]) == (st.MINISTRY_PAID, 8500)


def test_a_school_payment_they_paid_themselves_is_owed_back():
    [row] = st.month_lines([_school("85.00", "USD", paid_by="own")], "2026-08")
    assert row["role"] == st.OWED


def test_the_school_list_says_who_paid():
    rows = st.parse_school(
        "Student,Amount,Date,School,Paid by\n"
        "Asha Gurung,85.00,2026-08-07,Shree Janata School,me\n"
        "Bikash Tamang,85.00,2026-08-07,Shree Janata School,T2T\n"
        "Mina Rai,85.00,2026-08-07,Shree Janata School,\n"
    )
    assert [r.paid_by for r in rows] == ["own", "", ""]


def test_a_list_with_no_one_saying_they_paid_keeps_each_rows_old_sha():
    row = st.SchoolPayment("2026-08-07", "Asha Gurung", "Shree Janata School", Decimal(85), "USD")
    old = f"school:{PERSON}|2026-08-07|Asha Gurung|Shree Janata School|85|USD"
    assert "paid_by" not in row.payload(PERSON)
    assert row.sha(PERSON) == hashlib.sha256(old.encode()).hexdigest()


# ---- how old cash is -----------------------------------------------------------------


def test_only_the_ministrys_cash_grows_old():
    own = _atm("2026-08-01", 8000, "NPR10000.00", st.OWN)
    assert st.cash_age(own, "2026-10-01") == {}
    assert st.cash_age(_envelope(), "2026-10-01") == {
        (PERSON, "NPR"): ("2026-08-03", Decimal("4000.00"))
    }


# ---- through the door ----------------------------------------------------------------

OWN_ATM = "Date,Description,Debit,Credit\n08/01/2026,ATM WITHDRAWAL 000777 KATHMANDU NP,80.00,\n"
MINISTRY_ATM = (
    "Date,Description,Debit,Credit\n08/03/2026,ATM WITHDRAWAL 000123 KATHMANDU NP,30.00,\n"
)


def test_the_card_and_the_report_owe_back_only_their_part(solo, feed):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, MINISTRY_ATM.encode())
    _send(solo, desk, token, OWN_ATM.encode())
    held = _held(door)
    door.call(
        "bank_statement", {"statement": held["2026-08-03"]["statement"], "account": "ministry"}
    )
    walked = door.call(
        "bank_statement", {"statement": held["2026-08-01"]["statement"], "account": "own"}
    )
    [atm] = [r["row"] for r in walked["rows"]]
    kept = door.call(
        "keep_own_lines",
        {"statement": held["2026-08-01"]["statement"], "rows": [atm], "said": "my cash"},
    )
    assert kept["status"] == "kept", kept
    bank = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    for line in bank:
        rupees = "10,000" if line["dollars"] == "80.00" else "4,000"
        said = door.call("explain_bank_line", {"line": line["source"], "said": f"{rupees} rupees"})
        assert said["status"] == "set", said
    _receipt_on(door, desk, token, solo, "2026-08-07", "6,000 rupees", JPEG + b"m")

    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    [receipt] = month["lines"]
    assert receipt["dollars"] == "46.00"
    assert receipt["paid"] == "from the ministry's cash and $16.00 of your own"
    assert month["total"] == "16.00"

    _month(solo, "2026-08")
    sent = door.call("send_month_for_review", {"month": "2026-08"})
    assert sent["status"] == "sent", sent
    [card] = [c for c in _cards(solo, "2026-08") if "lines" in c]
    assert card["total_cents"] == "1600"
    assert "from the ministry's cash and $16.00 of their own, owed back" in card["lines"]

    # Their yes builds a report of what's owed back: their own NPR 2,000, $16.00.
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute("UPDATE approval_queue SET status = 'approved' WHERE id = ?", (card["id"],))
    con.commit()
    con.close()
    built = _month(solo, "2026-08")
    [manifest] = [
        json.loads(p.read_text()) for p in Path(solo["root"]).parent.rglob("manifest.json")
    ]
    [line] = manifest["lines"]
    assert line["amount_cents"] == 1600 and "their own cash" in line["note"], built
    assert line["said_amount"] == ""  # the rate priced the whole purchase, not their part


def test_a_split_of_their_own_cash_is_reported_and_kept_part_by_part(solo, feed):  # noqa: F811
    # Review, 2026-11-10: the card priced every part at the whole line, and the
    # ledger kept one part of a split because its parts shared one key.
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_ATM.encode())
    held = _held(door)
    walked = door.call(
        "bank_statement", {"statement": held["2026-08-01"]["statement"], "account": "own"}
    )
    [atm] = [r["row"] for r in walked["rows"]]
    door.call(
        "keep_own_lines",
        {"statement": held["2026-08-01"]["statement"], "rows": [atm], "said": "my cash"},
    )
    [line] = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    assert (
        door.call("explain_bank_line", {"line": line["source"], "said": "10,000 rupees"})["status"]
        == "set"
    )
    counted = door.call("count_cash", {"said": "I've got 5,000 rupees left"})
    assert counted["status"] in ("set", "gap", "asks"), counted
    used = door.call(
        "explain_cash", {"said": "5,000 rupees went to the village visit", "whose": "ministry"}
    )
    assert used["status"] == "set", (counted, used)
    # Cash said today is spent today: it reads in the month now running.
    this = door.call("month_report", {})["month"]
    bank = door.call("month_report", {"month": this})["people"][0]["bank"]
    [spent] = [b for b in bank if b.get("dollars") == "40.00"]
    split = door.call(
        "split_line",
        {
            "line": spent["source"],
            "said": "2,000 rupees of that was supplies",
            "category": "ministry supplies",
        },
    )
    assert split["status"] == "set", split

    _month(solo, this)
    assert door.call("send_month_for_review", {"month": this})["status"] == "sent"
    [card] = [c for c in _cards(solo, this) if "lines" in c]
    assert card["total_cents"] == "4000"  # owed back once, never once per part
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute("UPDATE approval_queue SET status = 'approved' WHERE id = ?", (card["id"],))
    con.commit()
    _month(solo, this)
    [manifest] = [
        json.loads(p.read_text()) for p in Path(solo["root"]).parent.rglob("manifest.json")
    ]
    # The 2,000 rupees they took out; the rest stays where it was (2026-11-10).
    assert sorted((ln["category"], ln["amount_cents"]) for ln in manifest["lines"]) == [
        ("Ministry Supplies", 1600),
        ("Other", 2400),
    ]
    kept = con.execute("SELECT category, amount_cents FROM expense_line ORDER BY id").fetchall()
    con.close()
    assert sorted(kept) == [("Ministry Supplies", 1600), ("Other", 2400)]
