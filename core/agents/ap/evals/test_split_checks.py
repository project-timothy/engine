# ruff: noqa: F811 (the shared fixtures arrive as test parameters)
"""One bill, two checks (issue #688, 2026-11-18).

The bank's bill pay caps a check at $10,000, so a larger bill goes out as
two checks against ONE ledger row and ONE accounting-system bill (the live
shapes: a supplier's 9056 + 9057 in August, a machine shop 9170 + 9171 in November). The
owner records the split in the row's check reference, one check and its
amount at a time: "Checks 9170 + 9171 (bill pay: 9170 $10,000.00 on 11/24,
9171 $3,253.00 on 11/25)".

Contract under test:

- the reference is read by code, never guessed: each check named with its
  amount, every amount accounted for, the amounts summing to the row to the
  cent; anything short of that is a named problem, never a partial reading;
- ``qbo-push-payments`` writes ONE BillPayment PER CHECK, each a partial
  payment of the same bill (its own check number as DocNumber, its own
  amount, its own date), so each bank line arrives with its own exact
  match; the row carries every payment id; a re-run writes nothing;
- a split the reference does not spell out is skipped on the card with the
  reason, never written as one lump payment (the #688 defect: one $13,253
  record no bank line could match);
- on the statement, a line paying one piece is explained (never unknown
  money) and the row waits; when every piece has cleared, in one statement
  or across several, the row settles with the last clearing date, its
  reference intact, and one ``ap.reconcile.paid`` per piece naming that
  piece's own BillPayment (the record the auditor verifies);
- a line that names a piece's check at a different amount parks review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents.ap import store

# The payment-record evals' fake accounting system and flag, shared, not copied.
from core.agents.ap.evals.test_qbo_push_payments import (  # noqa: F401 (pytest fixtures)
    fake_qbo,
    payment_records_on,
)
from core.agents.ap.split_checks import read_split
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

pytestmark = pytest.mark.usefixtures("demo_without_authority")

VENDOR = "Acme Tooling"
CARD = "ap.qbo_payment_batch"
PAID = "ap.reconcile.paid"
UNKNOWN = "ap.reconcile.unknown"
REVIEW = "ap.reconcile_review"
LIVE_REF = "Checks 9170 + 9171 (5/3 bill pay: 9170 $10,000.00 on 11/24, 9171 $3,253.00 on 11/25)"
HEADER = "Posted,Memo,Chk,Value\n"


# ---- reading the reference ---------------------------------------------------


def test_the_live_reference_reads_as_two_pieces_with_their_own_dates():
    split = read_split(LIVE_REF, total_cents=1325300, base_date="2026-11-24")

    assert split is not None and split.ok, split
    assert [(p.check, p.amount_cents, p.send_date) for p in split.pieces] == [
        ("9170", 1000000, "2026-11-24"),
        ("9171", 325300, "2026-11-25"),
    ]


def test_one_check_is_not_a_split():
    assert read_split("9173", total_cents=51234, base_date="2026-11-18") is None
    assert read_split("Check 9025 (cleared 4/3)", total_cents=100, base_date="") is None


def test_an_amount_is_never_read_as_a_check_number():
    split = read_split("9080 $12000.00 + 9081 $500.00", total_cents=1250000, base_date="")
    assert split is not None and split.ok
    assert split.checks == ("9080", "9081")


def test_two_checks_without_amounts_is_a_named_problem():
    split = read_split("9056+9057", total_cents=1325000, base_date="2026-08-05")

    assert split is not None and not split.ok
    assert "9056" in split.problem and "9057" in split.problem
    assert "$" in split.problem  # it says how to write the reference


def test_amounts_that_miss_the_bill_are_a_named_problem():
    split = read_split("9170 $10,000.00 + 9171 $3,250.00", total_cents=1325300, base_date="")

    assert split is not None and not split.ok
    assert "$13,250.00" in split.problem and "$13,253.00" in split.problem


def test_a_send_date_across_new_year_lands_in_the_next_year():
    split = read_split(
        "9090 $10,000.00 on 12/30 + 9091 $5.00 on 1/2", total_cents=1000500, base_date="2026-12-30"
    )
    assert [p.send_date for p in split.pieces] == ["2026-12-30", "2027-01-02"]


# ---- the write side ----------------------------------------------------------


def _seed(ledger_dir, *, ref=LIVE_REF, cents=1325300, number="JC-1", bill="B70"):
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        inv_id, _ = store.insert_invoice(
            ledger,
            tenant="demo",
            vendor=VENDOR,
            invoice_number=number,
            amount_cents=cents,
            status="Scheduled",
            invoice_date="2026-11-09",
        )
        store.record_payment_details(
            ledger, invoice_id=inv_id, payment_date="2026-11-24", check_ref=ref
        )
        store.record_qbo_ids(ledger, invoice_id=inv_id, bill_id=bill)
    return root


def _pay(ledger_dir):
    return run("demo", "ap", "qbo-push-payments", ledger_dir=ledger_dir)


def _approve_pending(root):
    with Ledger.open(root) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == CARD:
                ledger.resolve_approval("demo", card["id"], "approved")


def _cards(root, action_type=CARD, status=None):
    with Ledger.open(root) as ledger:
        return [
            c
            for c in ledger.list_approvals("demo", status=status)
            if c["action_type"] == action_type
        ]


def _row(root, number="JC-1"):
    with Ledger.open(root) as ledger:
        return store.invoices_by_number(ledger, "demo", number)[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _recorded_split(tmp_path, fake_qbo):
    d = tmp_path / "d"
    root = _seed(d)
    assert _pay(d).status == "needs_approval"
    _approve_pending(root)
    result = _pay(d)
    assert result.status == "ok", result.summary
    return d, root


def test_a_split_check_parks_one_card_entry_per_check(tmp_path, fake_qbo, payment_records_on):
    d = tmp_path / "d"
    root = _seed(d)

    result = _pay(d)

    assert result.status == "needs_approval"
    (card,) = _cards(root)
    params = json.loads(card["params_json"]) if "params_json" in card else card["params"]
    entries = sorted((c["check"], c["amount_cents"], c["payment_date"]) for c in params["checks"])
    assert entries == [("9170", 1000000, "2026-11-24"), ("9171", 325300, "2026-11-25")]
    assert fake_qbo.created == []


def test_each_check_becomes_its_own_partial_payment_of_the_one_bill(
    tmp_path, fake_qbo, payment_records_on
):
    d, root = _recorded_split(tmp_path, fake_qbo)

    assert len(fake_qbo.created) == 2
    by_doc = {p["DocNumber"]: p for p in fake_qbo.created}
    assert set(by_doc) == {"9170", "9171"}
    assert by_doc["9170"]["TotalAmt"] == 10000.00
    assert by_doc["9171"]["TotalAmt"] == 3253.00
    assert by_doc["9170"]["TxnDate"] == "2026-11-24"
    assert by_doc["9171"]["TxnDate"] == "2026-11-25"
    for payload in by_doc.values():
        (line,) = payload["Line"]
        assert line["Amount"] == payload["TotalAmt"]  # a partial payment, never the bill total
        assert line["LinkedTxn"] == [{"TxnId": "B70", "TxnType": "Bill"}]
    row = _row(root)
    assert set(row["qbo_payment_id"].split(",")) == {"BillPayment:BP1", "BillPayment:BP2"}
    assert row["status"] == "Scheduled"  # recording is never settling
    assert row["check_ref"] == LIVE_REF


def test_a_recorded_split_never_writes_again(tmp_path, fake_qbo, payment_records_on):
    d, root = _recorded_split(tmp_path, fake_qbo)

    again = _pay(d)

    assert again.status in ("ok", "noop"), again.summary
    assert len(fake_qbo.created) == 2
    assert _cards(root, status="pending") == []


def test_the_engine_knows_both_pieces_as_its_own_records(tmp_path, fake_qbo, payment_records_on):
    _, root = _recorded_split(tmp_path, fake_qbo)
    with Ledger.open(root) as ledger:
        mine = store.engine_authored_qbo_ids(ledger, "demo")
    assert {"BillPayment:BP1", "BillPayment:BP2"} <= mine


def test_a_split_without_amounts_is_skipped_never_written_as_one_lump(
    tmp_path, fake_qbo, payment_records_on
):
    d = tmp_path / "d"
    root = _seed(d, ref="Checks 9170 + 9171")

    result = _pay(d)

    assert fake_qbo.created == []
    assert all("9170" not in a or "skipped" in a for a in result.actions)
    assert any("skipped" in a and "9170" in a and "9171" in a for a in result.actions)
    assert _cards(root) == []  # nothing else to record, so no card


# ---- the statement side -------------------------------------------------------


def _csv(tmp_path: Path, *lines, name="statement.csv") -> Path:
    p = tmp_path / name
    p.write_text(HEADER + "".join(f"{d},{m},{c},{v}\n" for d, m, c, v in lines))
    return p


def _reconcile(ledger_dir, tmp_path, csv):
    ev = tmp_path / "evidence.json"
    ev.write_text("[]")
    return run(
        "demo",
        "ap",
        "reconcile",
        params={"evidence_file": str(ev), "bank_csv": str(csv)},
        ledger_dir=ledger_dir,
    )


def test_one_piece_clearing_is_explained_and_the_row_waits(tmp_path, fake_qbo, payment_records_on):
    d, root = _recorded_split(tmp_path, fake_qbo)
    csv = _csv(tmp_path, ("2026-11-30", "CHECK 9170", "9170", "-10000.00"))

    result = _reconcile(d, tmp_path, csv)

    assert result.status == "ok", result.summary
    assert _row(root)["status"] == "Scheduled"
    assert _events(root, UNKNOWN) == []
    assert _events(root, PAID) == []
    assert _cards(root, action_type=REVIEW) == []


def test_the_second_piece_on_a_later_statement_settles_the_row(
    tmp_path, fake_qbo, payment_records_on
):
    d, root = _recorded_split(tmp_path, fake_qbo)
    first = ("2026-11-30", "CHECK 9170", "9170", "-10000.00")
    _reconcile(d, tmp_path, _csv(tmp_path, first, name="nov.csv"))

    result = _reconcile(
        d,
        tmp_path,
        _csv(tmp_path, first, ("2026-12-03", "CHECK 9171", "9171", "-3253.00"), name="dec.csv"),
    )

    assert result.status == "ok", result.summary
    row = _row(root)
    assert row["status"] == "Paid"
    assert row["payment_date"] == "2026-12-03"  # the last clearing
    assert row["check_ref"] == LIVE_REF  # both numbers stay on the row
    paid = _events(root, PAID)
    assert sorted((p["payload"]["check_ref"], p["payload"]["qbo_id"]) for p in paid) == [
        ("9170", "BillPayment:BP1"),
        ("9171", "BillPayment:BP2"),
    ]
    assert sorted(p["payload"]["amount_cents"] for p in paid) == [325300, 1000000]
    assert _events(root, UNKNOWN) == []

    again = _reconcile(
        d,
        tmp_path,
        _csv(tmp_path, first, ("2026-12-03", "CHECK 9171", "9171", "-3253.00"), name="dec.csv"),
    )
    assert again.status in ("ok", "noop")
    assert len(_events(root, PAID)) == 2  # settled once


def test_both_pieces_on_one_statement_settle_in_one_run(tmp_path, fake_qbo, payment_records_on):
    d, root = _recorded_split(tmp_path, fake_qbo)
    csv = _csv(
        tmp_path,
        ("2026-12-01", "CHECK 9171", "9171", "-3253.00"),
        ("2026-12-02", "CHECK 9170", "9170", "-10000.00"),
    )

    _reconcile(d, tmp_path, csv)

    row = _row(root)
    assert row["status"] == "Paid"
    assert row["payment_date"] == "2026-12-02"
    assert len(_events(root, PAID)) == 2


def test_a_piece_check_at_the_wrong_amount_parks_review(tmp_path, fake_qbo, payment_records_on):
    d, root = _recorded_split(tmp_path, fake_qbo)
    csv = _csv(tmp_path, ("2026-11-30", "CHECK 9170", "9170", "-9000.00"))

    _reconcile(d, tmp_path, csv)

    assert _row(root)["status"] == "Scheduled"
    assert len(_cards(root, action_type=REVIEW, status="pending")) == 1
    assert _events(root, UNKNOWN) == []
