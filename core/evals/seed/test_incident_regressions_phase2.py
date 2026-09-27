"""Seed evals: non-payment incidents that exercise later-phase modules.

Three incidents from the corpus, each as a skipped case with its fixture and
assertion built, naming the Phase 2+ module that must ship to unskip it.
"""

from __future__ import annotations

import pytest

DEDUP = "core/agents/expenses/dedup.py (amount+date receipt dedup)"
CLASSIFY = "core/agents/expenses/classify.py (reference-vs-receipt detector)"
CUSTOMERS = "core/agents/ar/customers.py (per-customer terms, no single-customer default)"


@pytest.mark.phase2
def test_amount_and_date_dedup_catches_separate_scans(seed_fixture):
    # 2026-05-18: the same paper receipt scanned twice has different bytes
    # (different MD5), so file-hash dedup misses it. Amount + date (within two
    # days) must catch it.
    data = seed_fixture("expenses_receipts.json")
    from core.agents.expenses.dedup import duplicate_groups

    groups = duplicate_groups(data["incoming"], day_window=2)
    flagged = {frozenset(g) for g in groups}
    assert frozenset({"scan_a.jpg", "scan_b.jpg"}) in flagged


@pytest.mark.phase2
def test_reference_image_is_not_treated_as_a_receipt(seed_fixture):
    # 2026-05-18: a single image with an empty body is reference material
    # (a bucketing map), not a receipt; it must not be OCR'd as a transaction.
    data = seed_fixture("expenses_receipts.json")
    from core.agents.expenses.classify import is_reference_material

    by_file = {item["file"]: item for item in data["incoming"]}
    assert is_reference_material(by_file["bucket_map.png"]) is True
    assert is_reference_material(by_file["scan_a.jpg"]) is False


@pytest.mark.phase2
@pytest.mark.skip(reason=f"Phase 2: {CUSTOMERS}")
def test_terms_resolve_per_customer_not_by_default(seed_fixture):
    # 2026-06-05: a second customer entered a system built for one. Net terms
    # must come from the project's customer, never a hardcoded default.
    data = seed_fixture("multi_customer.json")
    from core.agents.ar.customers import net_terms_for  # Phase 2

    by_pn = {p["pn"]: p for p in data["projects"]}
    assert net_terms_for(by_pn["P-2002"]) == 30
    assert net_terms_for(by_pn["P-2001"]) == 60
