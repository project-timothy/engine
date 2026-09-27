"""Unit tests for the AP vendor registry schema and resolution rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.ap.registry import VendorEntry, load_vendor_registry

SYNTHETIC = """\
["alphaparts.com"]
vendor = "Alpha Parts"
qbo_account = "Cost of Goods Sold:Project Expense"
cost_type = "Materials"
subject_aliases = ["alpha parts", "alphaparts"]
ledger_aliases = ["Alpha Parts Inc", "Alpha Parts LLC"]
payment_channel = "ACH"
tax_entity = "alpha-group-ein"

["alphafab"]
vendor = "Alpha Fabrication"
cost_type = "Subcontractors"
subject_aliases = ["alpha fab"]
tax_entity = "alpha-group-ein"

["solo.example"]
vendor = "Solo Vendor"
subject_aliases = ["solo"]
tax_classification = "s-corp"
tax_recipient = "Sole O. Proprietor"
"""


@pytest.fixture
def registry(tmp_path: Path):
    p = tmp_path / "vendors.toml"
    p.write_text(SYNTHETIC, encoding="utf-8")
    return load_vendor_registry(p)


def test_load_and_entry_shape(registry):
    assert set(registry.entries) == {"alphaparts.com", "alphafab", "solo.example"}
    entry = registry.entries["alphaparts.com"]
    assert isinstance(entry, VendorEntry)
    assert entry.vendor == "Alpha Parts"
    assert entry.payment_channel == "ACH"


def test_resolve_sender_by_domain_and_fragment(registry):
    # Full email address: matched on the domain part.
    assert registry.resolve_sender("billing@alphaparts.com").vendor == "Alpha Parts"
    # Stable name fragment key matches inside the domain (legacy convention:
    # keys like "weldingricks" match any sender domain containing them).
    assert registry.resolve_sender("ap@alphafab-shop.net").vendor == "Alpha Fabrication"
    assert registry.resolve_sender("nobody@unknown.org") is None


def test_resolve_subject_alias_case_insensitive(registry):
    assert registry.resolve_subject("FW: ALPHA FAB quote attached").vendor == "Alpha Fabrication"
    assert registry.resolve_subject("Invoice from Solo for May").vendor == "Solo Vendor"
    assert registry.resolve_subject("nothing relevant here") is None


def test_resolve_vendor_name(registry):
    assert registry.resolve_name("Alpha Parts").vendor == "Alpha Parts"
    assert registry.resolve_name("alpha parts").vendor == "Alpha Parts"
    assert registry.resolve_name("Nope Inc") is None


def test_resolve_ledger_name_matches_canonical_or_a_listed_alias(registry):
    # The legacy ledger sometimes spells a vendor differently than the engine.
    # resolve_ledger_name maps both the canonical name and any listed alias to
    # the one vendor, exactly and case-insensitively (2026-06-23 shadow finding).
    assert registry.resolve_ledger_name("Alpha Parts").vendor == "Alpha Parts"
    assert registry.resolve_ledger_name("alpha parts inc").vendor == "Alpha Parts"
    assert registry.resolve_ledger_name("ALPHA PARTS LLC").vendor == "Alpha Parts"


def test_resolve_ledger_name_never_merges_an_unlisted_name(registry):
    # The safety property: a name that is neither the canonical nor a listed
    # alias never resolves, so two distinct vendors cannot be silently merged.
    assert registry.resolve_ledger_name("Alpha Parts Holdings") is None
    assert registry.resolve_ledger_name("Beta Parts") is None


def test_tax_entity_siblings_one_ein_two_rows(registry):
    # Two distinct billing names under one taxpayer EIN stay distinct vendors
    # but are linked: payment verification may treat them as one tax entity.
    sibs = registry.tax_siblings("Alpha Parts")
    assert {e.vendor for e in sibs} == {"Alpha Parts", "Alpha Fabrication"}
    # A vendor with no tax_entity has no siblings beyond itself.
    assert [e.vendor for e in registry.tax_siblings("Solo Vendor")] == ["Solo Vendor"]


def test_tax_classification_field_loads_and_defaults_empty(registry):
    # The federal tax classification from the vendor's W-9 (e.g. "s-corp",
    # "c-corp"). Recorded so 1099 logic can exempt corporations; empty means
    # unknown/unstated, never a guess.
    assert registry.entries["solo.example"].tax_classification == "s-corp"
    assert registry.entries["alphaparts.com"].tax_classification == ""


def test_tax_recipient_field_loads_and_defaults_empty(registry):
    # The 1099-NEC line-1 name when it differs from the display name (a
    # disregarded SMLLC reports under its owner). Empty means the display
    # name IS the recipient. Names only; TINs never enter this repo.
    assert registry.entries["solo.example"].tax_recipient == "Sole O. Proprietor"
    assert registry.entries["alphaparts.com"].tax_recipient == ""


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_vendor_registry(tmp_path / "nope.toml")
