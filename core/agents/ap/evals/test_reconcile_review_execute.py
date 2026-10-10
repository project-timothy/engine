"""ap/reconcile: an approved ``ap.reconcile_review`` card EXECUTES (phase 7
row 7.1, issue #210).

Before this row a review card was record-only: the owner's approval marked
the clearing "explained" and nothing else happened, so the row he had just
resolved stayed Scheduled until somebody flipped it by hand. Now:

- approving with ``--param row=<id>`` flips exactly that row Paid on the
  next reconcile run, with the cleared date and check reference recorded,
  ``ap.reconcile.paid`` (the settle fact every consumer reads) and
  ``ap.invoice.paid`` (the owner-resolved row fact) emitted, and the other
  candidate untouched;
- approving WITHOUT the param is refused at the queue with a message naming
  the candidates, and the card stays pending (nothing half-approved);
- a row outside the candidates, a row already settled, or a cleared total
  that disagrees with the chosen row's amount is refused the same way
  (amount is a confirmation field, never the discriminator, 2026-05-21);
- re-running is a noop: the flip key and the event memory make the second
  run touch nothing and the third replay.

Evidence arrives via ``--param evidence_file`` so no eval touches the
network; approvals go through the real CLI so the queue-side check is the
one exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents.ap import store
from core.engine.cli import main
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

# Today's (no authority.toml) path; #435 adds the other.
pytestmark = pytest.mark.usefixtures("demo_without_authority")

VENDOR = "Acme Tooling"
CARD = "ap.reconcile_review"


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status, check_ref)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, check_ref in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
            )
            if check_ref:
                store.record_payment_details(ledger, invoice_id=inv_id, check_ref=check_ref)
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
        return store.invoices_by_number(ledger, "demo", number)[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _cards(root, status=None):
    with Ledger.open(root) as ledger:
        return [c for c in ledger.list_approvals("demo", status=status) if c["action_type"] == CARD]


def _history(root, invoice_id):
    with Ledger.open(root) as ledger:
        return [
            dict(r)
            for r in ledger.conn.execute(
                "SELECT * FROM ap_status_history WHERE invoice_id = ? ORDER BY id", (invoice_id,)
            ).fetchall()
        ]


def _approve(ledger_dir: Path, card_id: int, capsys, **params):
    argv = ["queue", "approve", "demo", "--id", str(card_id), "--ledger-dir", str(ledger_dir)]
    for k, v in params.items():
        argv += ["--param", f"{k}={v}"]
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


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


CLEARING = {
    "qbo_id": "Purchase:610",
    "txn_type": "Purchase",
    "payee": VENDOR,
    "amount_cents": 12500,
    "date": "2026-07-15",
    "check_ref": "9061",
}


def _ambiguous_world(tmp_path):
    """The acceptance fixture: one clearing, two same-amount open rows, one
    review card parked, nothing flipped."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "A-1", 12500, "Scheduled", ""),
        (VENDOR, "A-2", 12500, "Scheduled", ""),
    )
    ev = _evidence_file(tmp_path, CLEARING)
    parked = _run(d, ev)
    assert parked.status == "needs_approval"
    (card,) = _cards(root, "pending")
    return d, root, ev, card


# ---- the acceptance ----------------------------------------------------------


def test_approving_with_row_flips_exactly_that_row_with_the_clearing_evidence(tmp_path, capsys):
    d, root, ev, card = _ambiguous_world(tmp_path)
    chosen = _row(root, "A-2")
    other = _row(root, "A-1")

    code, out = _approve(d, card["id"], capsys, row=chosen["id"])
    assert code == 0, out
    result = _run(d, ev)

    assert result.status == "ok"
    assert result.anomalies == []
    flipped = _row(root, "A-2")
    assert flipped["status"] == "Paid"
    assert flipped["payment_date"] == "2026-07-15"
    assert flipped["check_ref"] == "9061"
    untouched = _row(root, "A-1")
    assert untouched["status"] == "Scheduled"
    assert untouched["payment_date"] in (None, "")
    assert len(_history(root, other["id"])) == 1  # birth only, no flip

    (invoice_paid,) = _events(root, "ap.invoice.paid")
    assert invoice_paid["payload"]["invoice_id"] == chosen["id"]
    assert invoice_paid["payload"]["qbo_ids"] == ["Purchase:610"]
    assert invoice_paid["payload"]["review_card"] == card["id"]
    assert invoice_paid["payload"]["payment_date"] == "2026-07-15"
    assert invoice_paid["payload"]["check_ref"] == "9061"
    # The settle fact the auditor's lenses and the explained-memory read:
    # one per cleared payment, carrying the payment's own amount.
    (settled,) = _events(root, "ap.reconcile.paid")
    assert settled["payload"]["invoice_id"] == chosen["id"]
    assert settled["payload"]["qbo_id"] == "Purchase:610"
    assert settled["payload"]["amount_cents"] == 12500
    assert settled["payload"]["review_card"] == card["id"]
    assert any("A-2" in a and "review card" in a for a in result.actions)
    # The flip is attributed to the owner's decision, not to engine guesswork.
    (birth, flip) = _history(root, chosen["id"])
    assert flip["status_to"] == "Paid"
    assert flip["actor"].startswith("owner")
    assert f"#{card['id']}" in flip["note"]


def test_approving_without_the_row_param_is_refused_naming_the_candidates(tmp_path, capsys):
    d, root, ev, card = _ambiguous_world(tmp_path)
    a1, a2 = _row(root, "A-1"), _row(root, "A-2")

    code, out = _approve(d, card["id"], capsys)

    assert code == 2
    assert "--param row=" in out
    assert f"#{a1['id']} A-1" in out
    assert f"#{a2['id']} A-2" in out
    # Nothing half-approved: the card is still pending and a run flips nothing.
    (pending,) = _cards(root, "pending")
    assert pending["id"] == card["id"]
    assert _cards(root, "approved") == []
    after = _run(d, ev)
    assert _row(root, "A-1")["status"] == "Scheduled"
    assert _row(root, "A-2")["status"] == "Scheduled"
    assert _events(root, "ap.invoice.paid") == []
    assert after.status in ("ok", "needs_approval", "noop")


def test_approving_a_row_outside_the_candidates_is_refused(tmp_path, capsys):
    d, root, ev, card = _ambiguous_world(tmp_path)
    a1, a2 = _row(root, "A-1"), _row(root, "A-2")

    code, out = _approve(d, card["id"], capsys, row=999)

    assert code == 2
    assert "999" in out
    assert f"#{a1['id']} A-1" in out
    assert f"#{a2['id']} A-2" in out
    assert _cards(root, "approved") == []

    code, out = _approve(d, card["id"], capsys, row="two")
    assert code == 2
    assert f"#{a2['id']} A-2" in out
    assert _cards(root, "approved") == []


def test_approving_a_candidate_that_already_settled_is_refused(tmp_path, capsys):
    """Between parking and approval somebody settled the row (a later
    clearing or a hand flip). Paid is terminal: the approval is refused
    rather than stacking a second settlement on the row."""
    d, root, ev, card = _ambiguous_world(tmp_path)
    a2 = _row(root, "A-2")
    with Ledger.open(root) as ledger:
        store.update_status(ledger, invoice_id=a2["id"], status_to="Paid", actor="owner")

    code, out = _approve(d, card["id"], capsys, row=a2["id"])

    assert code == 2
    assert "Paid" in out
    assert _cards(root, "approved") == []


def test_rerun_after_execution_is_a_noop(tmp_path, capsys):
    """The settling run changes the ledger, so the next run legitimately
    executes; it must find the card already executed and touch nothing (one
    flip, one event each); only then, inputs identical, does the runner
    replay a noop."""
    d, root, ev, card = _ambiguous_world(tmp_path)
    chosen = _row(root, "A-2")
    code, _ = _approve(d, card["id"], capsys, row=chosen["id"])
    assert code == 0

    first = _run(d, ev)
    second = _run(d, ev)
    third = _run(d, ev)

    assert first.status == "ok"
    assert second.status == "ok"
    assert second.anomalies == []
    assert second.approvals_needed == []
    assert third.status == "noop"
    assert len(_events(root, "ap.invoice.paid")) == 1
    assert len(_events(root, "ap.reconcile.paid")) == 1
    assert len(_history(root, chosen["id"])) == 2  # birth + one flip
    assert _row(root, "A-1")["status"] == "Scheduled"


def test_shadow_run_reports_the_execution_and_flips_nothing(tmp_path, capsys):
    d, root, ev, card = _ambiguous_world(tmp_path)
    chosen = _row(root, "A-2")
    code, _ = _approve(d, card["id"], capsys, row=chosen["id"])
    assert code == 0

    dry = _run(d, ev, shadow=True)

    assert dry.status == "ok"
    assert any("would settle" in a and "A-2" in a for a in dry.actions)
    assert _row(root, "A-2")["status"] == "Scheduled"
    assert _events(root, "ap.invoice.paid") == []

    live = _run(d, ev)

    assert live.status == "ok"
    assert _row(root, "A-2")["status"] == "Paid"


# ---- group cards (issue #115 shapes) share the one rule -----------------------


def test_group_card_whose_payments_sum_to_the_row_executes_on_approval(tmp_path, capsys):
    """Two unlinked partials to one vendor summing to the open row (the
    vendor-sum tier proposes, never settles). The owner confirms: the row
    settles with the LAST clearing date and every check reference, one
    settle event per payment with that payment's own amount (what the
    auditor's QBO lens verifies), one row-level paid event."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1127500, "Scheduled", ""))
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "7056", date="2026-08-05"),
        _billpayment("BillPayment:271", 127500, "7057", date="2026-08-07"),
    )
    parked = _run(d, ev)
    assert parked.status == "needs_approval"
    (card,) = _cards(root, "pending")
    row = _row(root, "B-44")

    code, out = _approve(d, card["id"], capsys, row=row["id"])
    assert code == 0, out
    result = _run(d, ev)

    assert result.status == "ok"
    assert result.anomalies == []
    settled = _row(root, "B-44")
    assert settled["status"] == "Paid"
    assert settled["payment_date"] == "2026-08-07"
    assert settled["check_ref"] == "7056+7057"
    paid = _events(root, "ap.reconcile.paid")
    assert {(e["payload"]["qbo_id"], e["payload"]["amount_cents"]) for e in paid} == {
        ("BillPayment:269", 1000000),
        ("BillPayment:271", 127500),
    }
    (invoice_paid,) = _events(root, "ap.invoice.paid")
    assert invoice_paid["payload"]["qbo_ids"] == ["BillPayment:269", "BillPayment:271"]
    assert invoice_paid["payload"]["amount_cents"] == 1127500


def test_group_card_short_of_the_row_is_refused_never_settles_a_partial(tmp_path, capsys):
    """Two linked partials cover $11,000 of an $11,250 row. Approving the
    card cannot make the row Paid (amount is a confirmation field): the
    approval is refused with both figures, the card stays pending, and the
    money keeps re-evaluating so the remainder's clearing settles the
    group on its own."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "B-44", 1127500, "Scheduled", ""))
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "B-44")[0]
        store.record_qbo_ids(ledger, invoice_id=row["id"], bill_id="777")
    ev = _evidence_file(
        tmp_path,
        _billpayment("BillPayment:269", 1000000, "7056", linked=["777"]),
        _billpayment("BillPayment:271", 100000, "7057", linked=["777"]),
    )
    parked = _run(d, ev)
    assert parked.status == "needs_approval"
    (card,) = _cards(root, "pending")
    row = _row(root, "B-44")

    code, out = _approve(d, card["id"], capsys, row=row["id"])

    assert code == 2
    assert "$11,000.00" in out
    assert "$11,275.00" in out
    (pending,) = _cards(root, "pending")
    assert pending["id"] == card["id"]
    assert _row(root, "B-44")["status"] == "Scheduled"
