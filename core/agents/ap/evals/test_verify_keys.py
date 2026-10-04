"""Verify verdicts are keyed on (payee, invoice_ref), never invoice_ref alone.

The 2026-08-20 assessment probe: two vendors sharing an invoice number
collapsed to ONE verdict entry — the committed row's verdict was silently
overwritten by its payable twin, and the payment-recommendation lookup
resolved the ref to the FIRST row carrying that number, attaching the wrong
vendor's amount to a "three-way verified payable" card. Invoice numbers are
vendor-scoped identifiers; two vendors both issuing "1001" is ordinary.
"""

from __future__ import annotations

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger


def _collision_state() -> dict:
    return {
        "ap_ledger": [
            # Vendor B's 1001: money COMMITTED (scheduled in bill pay).
            {
                "row": 1,
                "payee": "Beta Supply",
                "invoice_ref": "1001",
                "amount": 3800.0,
                "status": "Scheduled in bill pay",
                "check_ref": "",
            },
            # Vendor A's 1001: genuinely payable.
            {
                "row": 2,
                "payee": "Acme Tooling",
                "invoice_ref": "1001",
                "amount": 120.0,
                "status": "Received",
                "check_ref": "",
            },
        ],
        "cleared_bank": [],
        "billpay_queue": [],
    }


def test_ref_collision_keeps_both_verdicts():
    from core.agents.ap.verify import classify_payable

    verdict = classify_payable(_collision_state())
    assert len(verdict) == 2
    assert verdict[("Beta Supply", "1001")] == "committed"
    assert verdict[("Acme Tooling", "1001")] == "payable"


def test_payable_refs_carries_the_payee():
    from core.agents.ap.verify import payable_refs

    payable = set(payable_refs(_collision_state()))
    assert payable == {("Acme Tooling", "1001")}


def test_recommendation_card_attaches_the_right_row(tmp_path):
    """Job-level: the collision must not attach Beta's $3,800 to Acme's card."""
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Beta Supply",
            invoice_number="1001",
            amount_cents=380000,
            status="Scheduled in bill pay",
        )
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="1001",
            amount_cents=12000,
            status="Received",
        )
    billpay = tmp_path / "billpay.json"
    billpay.write_text("[]", encoding="utf-8")
    result = run(
        "demo",
        "ap",
        "verify-payment",
        shadow=True,
        params={"billpay": str(billpay)},
        ledger_dir=tmp_path,
    )
    cards = [a for a in result.approvals_needed if a.action_type == "ap.payment_recommendation"]
    assert len(cards) == 1
    assert cards[0].params["payee"] == "Acme Tooling"
    assert cards[0].params["invoice_ref"] == "1001"
    from decimal import Decimal

    assert Decimal(cards[0].params["amount"]) == Decimal("120")
