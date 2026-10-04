"""ap/reconcile: partial payments against one bill (issue #115 gap 1).

The 2026-08-12 incident class: one ledger row ($11,250), one QBO bill, paid
with two physical checks ($10,000 + $1,250) that the owner For-Review-matched
as two partial BillPayments against the one bill. Per-payment amounts match
no row total, so both landed ``ap.reconcile.unknown`` and the row kept aging
in the nightly audit for a week after the money had cleared. Worked around
by hand with ``engine status --paid``.

Contract under test:

- N BillPayments linking to the SAME QBO bill, where an open row carries
  that ``qbo_bill_id`` and the group sums to the row's amount, settle the
  row: one flip, payment details recorded (check refs joined "9056+9057"
  style), one ``ap.reconcile.paid`` event per payment so each is explained.
- Identity linkage with a sum that does NOT cover the row (a partial still
  in flight, or a mismatch) parks ONE review card naming every payment —
  never an unknown-money anomaly, never a flip.
- Without bill linkage, vendor+sum agreement alone is an amount-based join:
  it parks a review card proposing the group, never an auto-settle (amount
  is a confirmation field, never the discriminator — 2026-05-21).
- A group cleared before the row was committed parks for review
  (chronology guard, same rule as single payments).
- Re-runs after a group settle recognize the payments as explained.

Evidence arrives via ``--param evidence_file`` so no eval touches the
network.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status, qbo_bill_id)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, bill_id in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
            )
            if bill_id:
                store.record_qbo_ids(ledger, invoice_id=inv_id, bill_id=bill_id)
    return root


def _evidence_file(tmp_path: Path, *entries) -> Path:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(list(entries)))
    return p


def _run(ledger_dir: Path, evidence: Path, *, shadow: bool = False):
    return run(
        "demo",
        "ap",
        "reconcile",
        shadow=shadow,
        params={"evidence_file": str(evidence)},
        ledger_dir=ledger_dir,
    )


def _row(root, number):
    with Ledger.open(root) as ledger:
        rows = store.invoices_by_number(ledger, "demo", number)
    return rows[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _billpayment(qbo_id, cents, check_ref, *, date="2026-08-05", linked=()):
    return {
        "qbo_id": qbo_id,
        "txn_type": "BillPayment",
        "payee": VENDOR,
        "amount_cents": cents,
        "date": date,
        "check_ref": check_ref,
        "linked_bill_ids": list(linked),
    }


def test_two_partials_linked_to_one_bill_settle_the_row(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Scheduled", "777"))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056", linked=["777"]),
        _billpayment("BillPayment:271", 125000, "9057", linked=["777"]),
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert result.anomalies == []
    row = _row(root, "B-44")
    assert row["status"] == "Paid"
    assert row["payment_date"] == "2026-08-05"
    assert row["check_ref"] == "9056+9057"
    paid = _events(root, "ap.reconcile.paid")
    assert {e["payload"]["qbo_id"] for e in paid} == {"BillPayment:269", "BillPayment:271"}


def test_partial_group_sum_short_parks_one_review_card_not_unknowns(tmp_path):
    """Both payments cleared but together cover only part of the row: park
    ONE card naming both, flip nothing, and never call it unknown money."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Scheduled", "777"))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056", linked=["777"]),
        _billpayment("BillPayment:271", 100000, "9057", linked=["777"]),
    )

    result = _run(d, ev)

    assert result.status == "needs_approval"
    assert not any(a.code == "ap.reconcile.unknown_payment" for a in result.anomalies)
    (card,) = result.approvals_needed
    assert card.action_type == "ap.reconcile_review"
    assert "BillPayment:269" in card.params["qbo_ids"]
    assert "BillPayment:271" in card.params["qbo_ids"]
    assert _row(root, "B-44")["status"] == "Scheduled"


def test_single_partial_linked_payment_parks_review_not_unknown(tmp_path):
    """The interim state: the first check cleared, the second is in the
    mail. The money is explained (it pays a known bill), so a review card,
    never an unknown-money anomaly; the row stays open for the remainder."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Scheduled", "777"))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056", linked=["777"]),
    )

    result = _run(d, ev)

    assert result.status == "needs_approval"
    assert not any(a.code == "ap.reconcile.unknown_payment" for a in result.anomalies)
    (card,) = result.approvals_needed
    assert card.action_type == "ap.reconcile_review"
    assert _row(root, "B-44")["status"] == "Scheduled"


def test_vendor_sum_group_without_linkage_parks_review_never_settles(tmp_path):
    """No bill linkage means the join is vendor+sum: an amount-based match.
    Propose the group on a card; the engine never settles on amounts alone."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Scheduled", ""))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056"),
        _billpayment("BillPayment:271", 125000, "9057"),
    )

    result = _run(d, ev)

    assert result.status == "needs_approval"
    (card,) = result.approvals_needed
    assert card.action_type == "ap.reconcile_review"
    assert "BillPayment:269" in card.params["qbo_ids"]
    assert _row(root, "B-44")["status"] == "Scheduled"
    assert _events(root, "ap.reconcile.paid") == []


def test_group_cleared_before_commitment_parks_for_review(tmp_path):
    """Chronology guard applies to groups exactly as to single payments: a
    pair of clearings that predate the row's commitment cannot be its
    payment."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Received", "777"))
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "B-44")[0]
        store.update_status(ledger, invoice_id=row["id"], status_to="Scheduled", actor="owner")
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056", date="2020-01-01", linked=["777"]),
        _billpayment("BillPayment:271", 125000, "9057", date="2020-01-02", linked=["777"]),
    )

    result = _run(d, ev)

    assert result.status == "needs_approval"
    assert _row(root, "B-44")["status"] == "Scheduled"


def test_group_settle_rerun_recognizes_explained_then_noops(tmp_path):
    """The settling run changes the ledger, so the next run executes — but
    must see both payments as explained (no duplicate flip, no unknowns).
    Only then does an identical run replay a noop."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Scheduled", "777"))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056", linked=["777"]),
        _billpayment("BillPayment:271", 125000, "9057", linked=["777"]),
    )

    first = _run(d, ev)
    second = _run(d, ev)
    third = _run(d, ev)

    assert first.status == "ok"
    assert second.status == "ok"
    assert second.anomalies == []
    assert len(_events(root, "ap.reconcile.paid")) == 2
    assert third.status == "noop"


def test_group_settle_in_shadow_flips_nothing(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1125000, "Scheduled", "777"))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "9056", linked=["777"]),
        _billpayment("BillPayment:271", 125000, "9057", linked=["777"]),
    )

    dry = _run(d, ev, shadow=True)

    assert dry.status == "ok"
    assert _row(root, "B-44")["status"] == "Scheduled"
    assert _events(root, "ap.reconcile.paid") == []
    assert any("would settle" in a for a in dry.actions)

    live = _run(d, ev)

    assert live.status == "ok"
    assert _row(root, "B-44")["status"] == "Paid"
