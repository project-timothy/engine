"""Unit tests for the shadow parity diff (synthetic legacy workbook)."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from core.agents.ap.registry import VendorEntry, VendorRegistry
from core.agents.ap.shadow_diff import (
    LegacyRow,
    diff,
    read_legacy_rows,
    render_markdown,
    run_shadow_diff,
)
from core.engine.guard import WriteGuard

HEADER = [
    "Vendor", "Invoice #", "Invoice Date", "Due Date", "Amount", "QBO Account",
    "Cost Type", "Project/Area", "Status", "Payment Date", "Check/Ref #",
    "Notes", "Invoice PDF", "Confidence", "QBO_Bill_ID",
]  # fmt: skip


def _legacy_xlsx(tmp_path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "AP Ledger"
    ws.append(HEADER)
    ws.append(["OUTSTANDING — needs scheduling for payment"] + [None] * 14)
    ws.append(["Alpha Parts", "1001", "2026-06-01", None, 450.00, "", "", "",
               "Outstanding", None, "", "", "", "", ""])  # fmt: skip
    ws.append(["SCHEDULED — payment committed"] + [None] * 14)
    ws.append(["Alpha Parts", "1002", "2026-06-02", None, 5200.00, "", "", "",
               "Scheduled in bill pay", None, "", "", "", "", ""])  # fmt: skip
    ws.append(["PAID / VOID / DUPLICATE — settled"] + [None] * 14)
    ws.append(["Old Vendor", "7", "2026-01-15", None, 99.00, "", "", "",
               "Paid", "2026-01-20", "CK 9001", "", "", "", ""])  # fmt: skip
    path = tmp_path / "legacy.xlsx"
    wb.save(str(path))
    return path


def test_read_legacy_rows_skips_divider_bands(tmp_path):
    rows = read_legacy_rows(_legacy_xlsx(tmp_path))
    assert [(r.vendor, r.invoice_number) for r in rows] == [
        ("Alpha Parts", "1001"),
        ("Alpha Parts", "1002"),
        ("Old Vendor", "7"),
    ]
    assert rows[0].amount_cents == 45000


def test_diff_parity_when_rows_agree(tmp_path):
    legacy = read_legacy_rows(_legacy_xlsx(tmp_path))
    engine = [
        {"vendor": "Alpha Parts", "invoice_number": "1001", "amount_cents": 45000,
         "status": "Received", "invoice_date": "2026-06-01"},
        {"vendor": "Alpha Parts", "invoice_number": "1002", "amount_cents": 520000,
         "status": "Scheduled in bill pay", "invoice_date": "2026-06-02"},
    ]  # fmt: skip
    # 1001: engine Received vs legacy Outstanding = same payable group -> match.
    # Old Vendor / 7 predates the window -> not a parity miss.
    report = diff(engine, legacy, since="2026-06-01")
    assert report.is_parity, (report.mismatches, report.engine_only, report.legacy_only)
    assert report.matched == 2
    assert "PARITY" in render_markdown(report, tenant="t", since="2026-06-01")


def test_undated_legacy_rows_bucket_separately_and_do_not_break_parity(tmp_path):
    # Recurring auto-debits, CC paydowns, and contractor time rows carry no
    # invoice date in the legacy ledger. They cannot be windowed, so they get
    # their own one-time-review bucket instead of flooding the daily verdict.
    legacy = read_legacy_rows(_legacy_xlsx(tmp_path))
    legacy.append(
        type(legacy[0])(
            vendor="Recurring Vendor",
            invoice_number="",
            amount_cents=None,
            status="Paid",
            invoice_date="",
        )
    )
    engine = [
        {"vendor": "Alpha Parts", "invoice_number": "1001", "amount_cents": 45000,
         "status": "Received", "invoice_date": "2026-06-01"},
        {"vendor": "Alpha Parts", "invoice_number": "1002", "amount_cents": 520000,
         "status": "Scheduled in bill pay", "invoice_date": "2026-06-02"},
    ]  # fmt: skip
    report = diff(engine, legacy, since="2026-06-01")
    assert report.is_parity  # the undated row does not block the verdict
    assert len(report.undated_legacy) == 1
    rendered = render_markdown(report, tenant="t", since="2026-06-01")
    assert "PARITY" in rendered
    assert "Undated legacy rows" in rendered


def test_diff_flags_amount_status_and_missing_rows(tmp_path):
    legacy = read_legacy_rows(_legacy_xlsx(tmp_path))
    engine = [
        # Wrong amount.
        {"vendor": "Alpha Parts", "invoice_number": "1001", "amount_cents": 46000,
         "status": "Received", "invoice_date": "2026-06-01"},
        # Status group mismatch: engine payable vs legacy committed.
        {"vendor": "Alpha Parts", "invoice_number": "1002", "amount_cents": 520000,
         "status": "Received", "invoice_date": "2026-06-02"},
        # Row the legacy ledger does not have.
        {"vendor": "Beta Freight", "invoice_number": "88", "amount_cents": 100,
         "status": "Received", "invoice_date": "2026-06-03"},
    ]  # fmt: skip
    # Window includes 2026-01: the unmatched legacy Paid row counts as missed.
    report = diff(engine, legacy, since="2026-01-01")
    assert len(report.mismatches) == 2
    assert len(report.engine_only) == 1
    assert len(report.legacy_only) == 1
    rendered = render_markdown(report, tenant="t", since="2026-01-01")
    assert "NEEDS ATTENTION" in rendered


def test_diff_matches_vendor_with_parenthetical_alias(tmp_path):
    # 2026-06-16 shadow finding: the engine extractor recorded the vendor as
    # "Acme Safety (ASI)" while the legacy ledger carried "Acme Safety" for the
    # same invoice number. Identity must not split on a trailing parenthetical
    # alias, or one real invoice double-counts as both engine-only AND
    # legacy-only and the day reads NEEDS ATTENTION for a cosmetic difference.
    wb = Workbook()
    ws = wb.active
    ws.title = "AP Ledger"
    ws.append(HEADER)
    ws.append(["Acme Safety", "V-04", "2026-06-15", None, 11275.00, "", "", "",
               "Received", None, "", "", "", "", ""])  # fmt: skip
    path = tmp_path / "legacy_alias.xlsx"
    wb.save(str(path))

    legacy = read_legacy_rows(path)
    engine = [
        {"vendor": "Acme Safety (ASI)", "invoice_number": "V-04",
         "amount_cents": 1127500, "status": "Received", "invoice_date": "2026-06-15"},
    ]  # fmt: skip
    report = diff(engine, legacy, since="2026-06-10")
    assert report.is_parity, (report.engine_only, report.legacy_only)
    assert report.matched == 1
    assert not report.engine_only and not report.legacy_only


def test_canonical_vendor_keeps_non_alias_parentheticals_distinct(tmp_path):
    # The alias strip must not over-merge: two genuinely different vendors that
    # happen to share a leading token stay separate rows. The canonicalizer now
    # lives in registry (shared with three-way verification) but the diff's
    # behavior is unchanged.
    from core.agents.ap.registry import canonical_vendor

    assert canonical_vendor("Acme Safety (ASI)") == canonical_vendor("Acme Safety")
    assert canonical_vendor("Acme Safety") != canonical_vendor("Acme Freight")
    # A name that is only a parenthetical keeps its token rather than collapsing
    # to the empty string (which would merge every such row together).
    assert canonical_vendor("(unknown)") != canonical_vendor("(other)")


def test_diff_reconciles_one_vendor_under_two_spellings_via_ledger_alias():
    # A shadow-period finding: the engine files "Harbor Signs" while the legacy
    # ledger carries "Harbor Sign Systems" for the same invoice. With the
    # legacy spelling registered as a ledger alias, both resolve to one vendor
    # and the phantom engine-only/legacy-only pair collapses into a match.
    registry = VendorRegistry(
        entries={
            "harbor signs": VendorEntry(
                vendor="Harbor Signs", ledger_aliases=["Harbor Sign Systems"]
            ),
        }
    )
    legacy = [
        LegacyRow(
            vendor="Harbor Sign Systems",
            invoice_number="500123",
            amount_cents=4815,
            status="Received",
            invoice_date="2026-06-17",
        )
    ]
    engine = [
        {"vendor": "Harbor Signs", "invoice_number": "500123", "amount_cents": 4815,
         "status": "Received", "invoice_date": "2026-06-17"},
    ]  # fmt: skip
    report = diff(engine, legacy, since="2026-06-10", registry=registry)
    assert report.is_parity, (report.engine_only, report.legacy_only)
    assert report.matched == 1
    assert not report.alias_candidates  # already linked; nothing to confirm


def test_diff_suggests_an_alias_candidate_from_a_shared_invoice_number():
    # With no alias registered yet, the two spellings do not match, so the one
    # invoice double-counts. Because both sides carry the SAME invoice number,
    # the diff surfaces a confirm-to-link candidate, with no fuzzy name guessing.
    legacy = [
        LegacyRow(
            vendor="Harbor Sign Systems",
            invoice_number="500123",
            amount_cents=4815,
            status="Received",
            invoice_date="2026-06-17",
        )
    ]
    engine = [
        {"vendor": "Harbor Signs", "invoice_number": "500123", "amount_cents": 4815,
         "status": "Received", "invoice_date": "2026-06-17"},
    ]  # fmt: skip
    report = diff(engine, legacy, since="2026-06-10")  # no registry
    assert len(report.engine_only) == 1
    assert len(report.legacy_only) == 1
    assert len(report.alias_candidates) == 1
    candidate = report.alias_candidates[0]
    assert "500123" in candidate and "Harbor Sign Systems" in candidate
    rendered = render_markdown(report, tenant="t", since="2026-06-10")
    assert "Candidate vendor aliases" in rendered


def test_distinct_vendors_without_a_shared_invoice_are_not_candidates():
    # Two real engine-only / legacy-only rows that do not share an invoice
    # number must never be suggested as the same vendor.
    legacy = [
        LegacyRow(
            vendor="Beta Freight",
            invoice_number="88",
            amount_cents=100,
            status="Received",
            invoice_date="2026-06-12",
        )
    ]
    engine = [
        {"vendor": "Gamma Tools", "invoice_number": "99", "amount_cents": 200,
         "status": "Received", "invoice_date": "2026-06-12"},
    ]  # fmt: skip
    report = diff(engine, legacy, since="2026-06-10")
    assert len(report.engine_only) == 1
    assert len(report.legacy_only) == 1
    assert not report.alias_candidates


def test_registry_resolution_still_strips_a_parenthetical_canonical():
    # Regression (2026-06-24): when a vendor's registry canonical itself carries
    # a parenthetical, resolving through the registry must still match a legacy
    # spelling that drops it. The first cut returned the canonical verbatim,
    # which reintroduced the very split the 2026-06-16 parenthetical strip fixed.
    registry = VendorRegistry(entries={"acme": VendorEntry(vendor="Acme Safety (ASI)")})
    legacy = [
        LegacyRow(
            vendor="Acme Safety",
            invoice_number="V-9",
            amount_cents=100,
            status="Received",
            invoice_date="2026-06-15",
        )
    ]
    engine = [
        {"vendor": "Acme Safety (ASI)", "invoice_number": "V-9", "amount_cents": 100,
         "status": "Received", "invoice_date": "2026-06-15"},
    ]  # fmt: skip
    report = diff(engine, legacy, since="2026-06-10", registry=registry)
    assert report.is_parity, (report.engine_only, report.legacy_only)
    assert report.matched == 1
    assert not report.alias_candidates


def test_run_shadow_diff_writes_only_inside_allowed_root(tmp_path):
    from core.engine.runner import resolve_ledger_root, run

    legacy_path = _legacy_xlsx(tmp_path)
    landing = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"
    run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=tmp_path / "data",
    )
    from core.ledger import Ledger

    root = resolve_ledger_root("demo", tmp_path / "data")
    guard = WriteGuard([tmp_path / "protected"])
    with Ledger.open(root) as ledger:
        report, written = run_shadow_diff(
            tenant_slug="demo",
            ledger=ledger,
            legacy_xlsx=legacy_path,
            guard=guard,
            since="2026-06-01",
            out_path=root / "shadow-reports" / "parity-test.md",
        )
    assert written is not None and written.exists()
    assert "Verdict" in written.read_text(encoding="utf-8")
    # 1001 matches; 1002 is a real finding: fresh intake records Received
    # (payable) while the synthetic legacy row is already committed. The diff
    # exists to surface exactly this.
    assert report.matched == 1
    assert len(report.mismatches) == 1 and "1002" in report.mismatches[0]
