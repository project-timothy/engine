"""QBO-consistency lens evals: drift in either direction surfaces. A fake
client stands in for the accounting system; no eval opens a socket."""

from __future__ import annotations

from auditor.lenses import qbo

from .fixtures import add_event, add_invoice, make_context, make_ledger


class FakeQbo:
    """{entity: {id: amount_cents}} of what 'QBO' still holds."""

    def __init__(self, holdings):
        self.holdings = holdings
        self.calls = []

    def fetch_by_ids(self, entity, ids):
        self.calls.append((entity, sorted(ids)))
        held = self.holdings.get(entity, {})
        return {i: held[i] for i in ids if i in held}


def _check(tmp_path, holdings):
    ctx = make_context(tmp_path, qbo_token_file="~/nonexistent-tokens.json")
    with ctx.ledger:
        return qbo.check(ctx, client=FakeQbo(holdings))


def _conditions(findings):
    return sorted(f.condition for f in findings)


def test_agreeing_sides_are_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, amount_cents=55217, qbo_bill_id="239")
    add_event(
        conn,
        event_type="ap.qbo.bill_created",
        payload={"invoice_id": inv, "qbo_bill_id": "239", "amount_cents": 55217},
    )
    assert _check(tmp_path, {"Bill": {"239": 55217}}) == []


def test_vanished_bill_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    add_invoice(conn, amount_cents=55217, qbo_bill_id="239")
    findings = _check(tmp_path, {"Bill": {}})
    assert _conditions(findings) == ["bill-vanished"]
    assert findings[0].severity == "CRITICAL"


def test_edited_bill_amount_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    add_invoice(conn, amount_cents=55217, qbo_bill_id="239")
    findings = _check(tmp_path, {"Bill": {"239": 55200}})
    assert _conditions(findings) == ["bill-amount-drift"]
    assert findings[0].severity == "CRITICAL"


def test_write_event_and_row_disagreeing_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, amount_cents=55217, qbo_bill_id="240")  # row says 240
    add_event(
        conn,
        event_type="ap.qbo.bill_created",
        payload={"invoice_id": inv, "qbo_bill_id": "239", "amount_cents": 55217},
    )
    findings = _check(tmp_path, {"Bill": {"240": 55217}})
    assert _conditions(findings) == ["write-record-mismatch"]


def test_write_event_for_a_vanished_row_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    add_event(
        conn,
        event_type="ap.qbo.bill_created",
        payload={"invoice_id": 999, "qbo_bill_id": "239", "amount_cents": 55217},
    )
    findings = _check(tmp_path, {})
    assert _conditions(findings) == ["row-vanished"]


def test_vanished_reconcile_evidence_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid", payment_date="2026-07-02")
    add_event(
        conn,
        event_type="ap.reconcile.paid",
        payload={"invoice_id": inv, "qbo_id": "Purchase:226", "amount_cents": 10000},
    )
    findings = _check(tmp_path, {"Purchase": {}})
    assert _conditions(findings) == ["evidence-vanished"]
    assert findings[0].severity == "CRITICAL"


def test_shrunken_evidence_warns(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid", payment_date="2026-07-02", amount_cents=10000)
    add_event(
        conn,
        event_type="ap.reconcile.paid",
        payload={"invoice_id": inv, "qbo_id": "Purchase:226", "amount_cents": 10000},
    )
    findings = _check(tmp_path, {"Purchase": {"226": 9000}})
    assert _conditions(findings) == ["evidence-amount-drift"]
    assert findings[0].severity == "WARN"


def test_bundled_evidence_larger_than_one_invoice_is_fine(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, status="Paid", payment_date="2026-07-02", amount_cents=10000)
    add_event(
        conn,
        event_type="ap.reconcile.paid",
        payload={"invoice_id": inv, "qbo_id": "BillPayment:9", "amount_cents": 10000},
    )
    assert _check(tmp_path, {"BillPayment": {"9": 25000}}) == []


def test_no_token_file_skips_external_checks_only(tmp_path):
    conn = make_ledger(tmp_path)
    inv = add_invoice(conn, amount_cents=55217, qbo_bill_id="240")
    add_event(
        conn,
        event_type="ap.qbo.bill_created",
        payload={"invoice_id": inv, "qbo_bill_id": "239", "amount_cents": 55217},
    )
    ctx = make_context(tmp_path, qbo_token_file="")
    with ctx.ledger:
        findings = qbo.check(ctx)  # no client, no token: local checks still run
    assert _conditions(findings) == ["write-record-mismatch"]
