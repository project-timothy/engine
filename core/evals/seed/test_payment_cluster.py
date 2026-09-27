"""Seed evals: the 2026-05-21 payment cluster.

This is where the money risk lives, so it gets the most coverage. The cluster:
a payment run verified items against the cleared bank register only, missed
the bill-pay scheduled/in-process queue, and nearly re-issued ~$38K already
committed; separately, two invoices for the same amount could not be told
apart by amount, and a bank-cleared check had no ledger row.

Every test below builds a synthetic fixture (``payment_state.json``) and the
exact assertion the engine must satisfy, against a Phase 2 API that does not
exist yet. They are skipped with the module that must ship to unskip them.
Unskipping these is Phase 2's definition of done (architecture 3.6, invariant
4: three-way verification is engine code, not agent judgment).
"""

from __future__ import annotations

import pytest

VERIFY = "core/agents/ap/verify.py (three-way payment verification)"
RECON = "core/agents/audit/bank_recon.py (orphan + ledger-status-lag checks)"
MATCH_KEYS = ["payee", "invoice_or_check_ref", "ledger_status"]


def test_three_way_verification_classifies_every_row(seed_fixture):
    state = seed_fixture("payment_state.json")
    from core.agents.ap.verify import classify_payable  # Phase 2

    verdict = classify_payable(state, match_keys=MATCH_KEYS)
    # A-1001 is in the bill-pay queue: committed money, never payable.
    assert verdict[("Vendor Alpha", "A-1001")] == "committed"
    # A-1002 shares A-1001's amount but is a distinct ref and sits in no queue.
    assert verdict[("Vendor Alpha", "A-1002")] == "payable"
    # B-77 is outstanding, uncleared, unqueued.
    assert verdict[("Vendor Beta", "B-77")] == "payable"
    # G-12 cleared the bank: already paid.
    assert verdict[("Vendor Gamma", "G-12")] == "paid"


def test_billpay_queue_is_consulted_not_just_cleared_register(seed_fixture):
    state = seed_fixture("payment_state.json")
    from core.agents.ap.verify import payable_refs  # Phase 2

    payable = set(payable_refs(state, match_keys=MATCH_KEYS))
    # The near-double-pay: A-1001 is uncleared but scheduled in bill pay.
    # Checking the cleared register alone would call it payable. It is not.
    assert ("Vendor Alpha", "A-1001") not in payable


def test_same_amount_distinct_ref_is_not_settled_by_collision(seed_fixture):
    state = seed_fixture("payment_state.json")
    from core.agents.ap.verify import payable_refs  # Phase 2

    payable = set(payable_refs(state, match_keys=MATCH_KEYS))
    # A-1002 must stay payable even though an equal-amount sibling is committed.
    assert ("Vendor Alpha", "A-1002") in payable


def test_matching_by_amount_alone_is_ambiguous(seed_fixture):
    state = seed_fixture("payment_state.json")
    from core.agents.ap.verify import AmbiguousMatch, match_payment  # Phase 2

    # Amount alone collides on the two $2000 Vendor Alpha invoices; resolving by
    # payee + invoice ref is unambiguous.
    with pytest.raises(AmbiguousMatch):
        match_payment(state, payee="Vendor Alpha", amount=2000.00)
    assert match_payment(state, payee="Vendor Alpha", invoice_ref="A-1002").row == 2


def test_orphan_bank_check_with_no_ledger_row_is_flagged(seed_fixture):
    state = seed_fixture("payment_state.json")
    from core.agents.audit.bank_recon import orphan_checks  # Phase 2

    orphans = {c["check_ref"] for c in orphan_checks(state)}
    # CK-9043 cleared the bank but has no AP ledger row: an unlogged payment.
    assert "CK-9043" in orphans
    # CK-9100 reconciles to G-12, so it is not an orphan.
    assert "CK-9100" not in orphans


def test_ledger_status_lag_surfaces_and_blocks_payable(seed_fixture):
    state = seed_fixture("payment_state.json")
    from core.agents.audit.bank_recon import status_lag_anomalies  # Phase 2

    lagging = {a["invoice_ref"] for a in status_lag_anomalies(state)}
    # G-12 reads "Received" while its check already cleared: the ledger lacks an
    # intermediate state, the signal that nearly drove the double-pay.
    assert "G-12" in lagging
