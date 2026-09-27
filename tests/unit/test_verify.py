"""Unit tests for three-way verification branches beyond the seed evals.

DoD item 3 names three synthetic failure cases the verification must flag:
missing bill-pay-queue entry, orphan cleared check, duplicate same-vendor
same-amount invoices. Each is here, plus the loose check-number matching the
legacy ledger's mixed formats require.
"""

from __future__ import annotations

import pytest

from core.agents.ap.registry import VendorEntry, VendorRegistry
from core.agents.ap.verify import (
    AmbiguousMatch,
    NoMatch,
    classify_payable,
    match_payment,
    payable_refs,
)
from core.agents.audit.bank_recon import orphan_checks, status_lag_anomalies


def _state():
    return {
        "ap_ledger": [
            {
                "row": 1,
                "payee": "V One",
                "invoice_ref": "I-1",
                "amount": 500.0,
                "status": "Outstanding",
            },
            {
                "row": 2,
                "payee": "V One",
                "invoice_ref": "I-2",
                "amount": 500.0,
                "status": "Outstanding",
            },
            {
                "row": 3,
                "payee": "V Two",
                "invoice_ref": "I-9",
                "amount": 750.0,
                "status": "Received",
                "check_ref": "Check 9034",
            },
        ],
        "cleared_bank": [],
        "billpay_queue": [],
    }


def test_missing_billpay_snapshot_means_committed_status_still_protects():
    # The queue snapshot is absent (the 2026-05-21 gap), but a row already in
    # a committed status must still never classify payable.
    state = _state()
    state["ap_ledger"][0]["status"] = "Scheduled in bill pay"
    verdict = classify_payable(state)
    assert verdict[("V One", "I-1")] == "committed"
    assert ("V One", "I-1") not in payable_refs(state)


def test_orphan_cleared_check_is_flagged_not_matched():
    state = _state()
    state["cleared_bank"].append({"check_ref": "CK-7777", "payee": "Ghost Vendor", "amount": 123.0})
    orphans = orphan_checks(state)
    assert [o["check_ref"] for o in orphans] == ["CK-7777"]


def test_duplicate_same_vendor_same_amount_never_collapses():
    state = _state()
    # A queue entry for V One with no invoice ref and the colliding $500
    # amount is ambiguous between I-1 and I-2: neither may silently match,
    # both stay payable, and match_payment refuses.
    state["billpay_queue"].append({"payee": "V One", "amount": 500.0, "state": "Scheduled"})
    verdict = classify_payable(state)
    assert verdict[("V One", "I-1")] == "payable"
    assert verdict[("V One", "I-2")] == "payable"
    with pytest.raises(AmbiguousMatch):
        match_payment(state, payee="V One", amount=500.0)


def test_loose_check_number_matching_absorbs_legacy_formats():
    # Bank says check 9034; the ledger's Check/Ref column says 'Check 9034'.
    state = _state()
    state["cleared_bank"].append({"check_ref": "9034", "amount": 750.0})
    assert orphan_checks(state) == []
    verdict = classify_payable(state)
    assert verdict[("V Two", "I-9")] == "paid"
    # And the lag detector sees the Received row whose check already cleared.
    lagging = {a["invoice_ref"]: a["evidence"] for a in status_lag_anomalies(state)}
    assert lagging.get("I-9") == "cleared_bank"


def test_amount_mismatch_blocks_a_payee_only_join():
    state = _state()
    # Unique payee candidate but the amount disagrees: confirmation fails.
    state["cleared_bank"].append({"payee": "V Two", "amount": 1.0})
    assert len(orphan_checks(state)) == 1


def test_match_payment_no_match_raises_cleanly():
    with pytest.raises(NoMatch):
        match_payment(_state(), payee="Nobody", invoice_ref="X")


def test_amount_only_match_keys_are_rejected():
    with pytest.raises(ValueError):
        classify_payable(_state(), match_keys=["amount"])


def test_ledger_alias_lets_a_committed_payment_match_the_engine_spelling():
    """The bill-pay/bank snapshot may spell a vendor differently than the engine
    files it (e.g. the ledger row is the engine's canonical name, the queue uses
    the legacy full name). Matching payee by exact string misses the row, so the
    committed payment goes undetected and the invoice reads payable: the exact
    ~$38K double-payment mechanism invariant 4 exists to prevent. The vendor
    registry's ledger_aliases reconcile the two spellings, the same
    canonicalization the shadow parity diff already trusts.
    """
    registry = VendorRegistry(
        entries={"acme": VendorEntry(vendor="Acme Manufacturing", ledger_aliases=["Acme Mfg Co"])}
    )
    state = {
        "ap_ledger": [
            {
                "row": 1,
                "payee": "Acme Manufacturing",  # engine's canonical spelling
                "invoice_ref": "778",
                "amount": 1200.0,
                "status": "Received",
            }
        ],
        "cleared_bank": [],
        # The queue lists the SAME invoice under the legacy spelling.
        "billpay_queue": [
            {"payee": "Acme Mfg Co", "invoice_ref": "778", "amount": 1200.0, "state": "Scheduled"}
        ],
    }
    # Exact-string matching misses the alias: the row reads payable (the bug).
    assert classify_payable(state)[("Acme Manufacturing", "778")] == "payable"
    # Registry canonicalization catches it: committed money, never re-paid.
    assert classify_payable(state, registry=registry)[("Acme Manufacturing", "778")] == "committed"
    assert ("Acme Manufacturing", "778") not in payable_refs(state, registry=registry)


def test_registry_never_merges_two_genuinely_distinct_vendors():
    """Canonicalization must not over-merge: two different vendors that share an
    amount but not identity stay separate, so a payment to one never marks the
    other committed.
    """
    registry = VendorRegistry(
        entries={
            "acme": VendorEntry(vendor="Acme Manufacturing", ledger_aliases=["Acme Mfg Co"]),
            "beta": VendorEntry(vendor="Beta Supply"),
        }
    )
    state = {
        "ap_ledger": [
            {"row": 1, "payee": "Beta Supply", "invoice_ref": "B-1", "amount": 1200.0,
             "status": "Received"},
        ],
        "cleared_bank": [],
        "billpay_queue": [
            {"payee": "Acme Mfg Co", "invoice_ref": "A-9", "amount": 1200.0, "state": "Scheduled"},
        ],
    }  # fmt: skip
    # The Acme queue entry must not touch the Beta row despite the equal amount.
    assert classify_payable(state, registry=registry)[("Beta Supply", "B-1")] == "payable"
