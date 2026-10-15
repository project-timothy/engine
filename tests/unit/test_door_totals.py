"""Tim hands over the sums an owner asks for (2026-10-12).

Replay on a busy day of real books (proving-ground #12): "What do I owe
right now?" drew a subtotal per vendor, and "How much have we been billed by
<vendor> in all?" a sum, both added up by the model. They were right, but
code computes money and the model quotes it. So open_payables carries a
subtotal per vendor, find_invoices a total, both over every matching bill
(not only the rows shown), with the count so a short list says it is short.
"""

from __future__ import annotations

import pytest

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.tools import catalog


@pytest.fixture
def books(tmp_path):
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status in (
            ("Acme Tooling", "A-1", 120000, "Received"),
            ("Acme Tooling", "A-2", 4550, "Scheduled"),
            ("Acme Tooling", "A-0", 9900, "Paid"),
            ("Birch Supply", "B-1", 200001, "Received"),
            ("Cedar Labs", "C-1", 1000, "Received"),
        ):
            store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
            )
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="SH-1",
            amount_cents=777,
            status="Received",
            shadow=True,
        )
    return root


def _tools(root):
    return catalog.Tools("demo", ledger_root=root, today="2026-10-12")


def test_what_is_owed_comes_with_a_subtotal_per_vendor(books):
    out = _tools(books).call("open_payables", {})
    assert out["total"] == "3255.51" and out["count"] == 4
    assert out["by_vendor"] == [
        {"vendor": "Birch Supply", "count": 1, "total": "2000.01"},
        {"vendor": "Acme Tooling", "count": 2, "total": "1245.50"},
        {"vendor": "Cedar Labs", "count": 1, "total": "10.00"},
    ]


def test_the_sums_cover_every_open_bill_not_only_the_rows_shown(books):
    out = _tools(books).call("open_payables", {"limit": 1})
    assert len(out["rows"]) == 1
    assert out["count"] == 4 and out["total"] == "3255.51"
    assert sum(v["count"] for v in out["by_vendor"]) == 4


def test_a_vendor_search_comes_with_its_total(books):
    out = _tools(books).call("find_invoices", {"query": "acme"})
    assert out["count"] == 3 and out["total"] == "1344.50"  # any status, never shadow
    short = _tools(books).call("find_invoices", {"query": "acme", "limit": 1})
    assert len(short["rows"]) == 1 and short["count"] == 3 and short["total"] == "1344.50"


def test_the_tool_text_says_to_quote_the_sums_and_keep_sources_out():
    specs = {s.name: s.description for s in catalog.SPECS}
    for name in ("open_payables", "find_invoices"):
        text = specs[name].lower()
        assert "quote" in text, name
        assert "source" in text and "never" in text, name
    assert "per vendor" in specs["open_payables"]


def test_a_vendor_search_splits_its_total_by_status(books):
    # Replay, second pass: "is it paid?" and "billed in all" drew a paid and an
    # unpaid sum per vendor, still added up by the model.
    out = _tools(books).call("find_invoices", {"query": "acme"})
    assert out["by_status"] == [
        {"status": "Received", "count": 1, "total": "1200.00"},
        {"status": "Paid", "count": 1, "total": "99.00"},
        {"status": "Scheduled", "count": 1, "total": "45.50"},
    ]
