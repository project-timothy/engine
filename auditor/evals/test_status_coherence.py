"""Status-coherence lens evals: a row's status must agree with its own story."""

from __future__ import annotations

from auditor.lenses import status_coherence

from .fixtures import add_event, add_history, add_invoice, make_context, make_ledger


def _conditions(findings):
    return sorted(f.condition for f in findings)


def _check(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.ledger:
        return status_coherence.check(ctx)


def test_clean_book_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid", payment_date="2026-07-01")
    add_history(conn, inv, "Received", "Scheduled")
    add_history(conn, inv, "Scheduled", "Paid")
    add_invoice(conn, invoice_number="INV-2", status="Received")
    assert _check(tmp_path) == []


def test_unknown_status_is_flagged(tmp_path):
    conn = make_ledger(tmp_path)
    add_invoice(conn, status="Sorta Paid")
    assert _conditions(_check(tmp_path)) == ["unknown-status"]


def test_row_and_history_disagreeing_is_flagged(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Received")
    add_history(conn, inv, "Received", "Scheduled")
    assert _conditions(_check(tmp_path)) == ["history-mismatch"]


def test_flip_after_settlement_is_flagged(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid", payment_date="2026-07-01")
    add_history(conn, inv, "Scheduled", "Paid")
    add_history(conn, inv, "Paid", "Paid")  # any entry after settling is wrong
    assert "settled-reopened" in _conditions(_check(tmp_path))


def test_paid_without_any_evidence_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid")
    add_history(conn, inv, "Scheduled", "Paid")
    findings = _check(tmp_path)
    assert _conditions(findings) == ["paid-without-evidence"]
    assert findings[0].severity == "CRITICAL"


def test_reconcile_event_is_evidence(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid")
    add_history(conn, inv, "Scheduled", "Paid")
    add_event(conn, event_type="ap.reconcile.paid", payload={"invoice_id": inv})
    assert _check(tmp_path) == []


def test_owner_flip_is_evidence(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid")
    add_history(conn, inv, "Scheduled", "Paid", actor="owner")
    assert _check(tmp_path) == []


def test_history_note_on_the_paid_flip_is_evidence(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid")
    add_history(conn, inv, "Scheduled", "Paid", note="wire confirmed by vendor")
    assert _check(tmp_path) == []


def test_payable_row_with_payment_fields_is_flagged(tmp_path):
    conn = make_ledger(tmp_path)
    add_invoice(conn, status="Received", check_ref="4042")
    assert _conditions(_check(tmp_path)) == ["payable-with-payment-fields"]


def test_committed_row_with_check_ref_is_normal(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Scheduled in bill pay", check_ref="4042")
    add_history(conn, inv, "Received", "Scheduled in bill pay")
    assert _check(tmp_path) == []
