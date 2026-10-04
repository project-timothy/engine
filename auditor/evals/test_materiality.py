"""Materiality-outlier lens evals (old A-AP-005, ported 2026-09-04).

A row over the floor AND over N times the vendor's trailing-window median
is an amount worth a second look: the OCR-shaped error ($14,881.75 read as
$148,855.00) approved in a batch and posted to QBO. The fingerprint keys on
(vendor, invoice number, amount to the cent), never the row id, so a dated
mute on it survives re-runs and never suppresses a different amount.
"""

from __future__ import annotations

from auditor.lenses import materiality

from .fixtures import add_invoice, make_context, make_ledger


def _history(conn, vendor="Acme", amounts=(120000, 130000, 125000), start_month=1):
    for i, cents in enumerate(amounts):
        add_invoice(
            conn,
            vendor=vendor,
            invoice_number=f"H-{i}",
            amount_cents=cents,
            invoice_date=f"2026-0{start_month + i}-10",
        )


def test_row_over_floor_and_over_multiple_of_median_warns(tmp_path):
    conn = make_ledger(tmp_path)
    _history(conn)
    add_invoice(
        conn, vendor="Acme", invoice_number="BIG", amount_cents=1488175, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = materiality.check(ctx)
    assert [(f.condition, f.severity) for f in findings] == [("amount-outlier", "WARN")]
    f = findings[0]
    assert f.subject == "Acme / BIG / $14,881.75"
    assert "median $1,250.00" in f.detail
    assert "11.9x" in f.detail
    assert f.fingerprint in f.detail  # the acknowledgment handle is printed


def test_row_under_the_floor_is_quiet_however_large_the_multiple(tmp_path):
    conn = make_ledger(tmp_path)
    _history(conn, amounts=(10000, 11000, 12000))
    add_invoice(
        conn, vendor="Acme", invoice_number="X", amount_cents=400000, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert materiality.check(ctx) == []


def test_row_over_floor_but_in_line_with_history_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    _history(conn, amounts=(900000, 950000, 980000))
    add_invoice(
        conn, vendor="Acme", invoice_number="X", amount_cents=1000000, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert materiality.check(ctx) == []


def test_no_peer_in_the_window_means_no_verdict(tmp_path):
    conn = make_ledger(tmp_path)
    add_invoice(
        conn, vendor="Acme", invoice_number="OLD", amount_cents=100000, invoice_date="2024-01-10"
    )
    add_invoice(
        conn, vendor="Acme", invoice_number="X", amount_cents=1000000, invoice_date="2026-07-01"
    )
    add_invoice(
        conn, vendor="Newco", invoice_number="1", amount_cents=2000000, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert materiality.check(ctx) == []


def test_median_not_mean_so_one_prior_outlier_cannot_lift_the_floor(tmp_path):
    conn = make_ledger(tmp_path)
    # the giant prior comes first (no peers of its own, so no verdict on it)
    _history(conn, amounts=(9000000, 100000, 100000, 100000))
    add_invoice(
        conn, vendor="Acme", invoice_number="X", amount_cents=600000, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = materiality.check(ctx)
    assert [f.subject for f in findings] == ["Acme / X / $6,000.00"]


def test_only_priors_count_and_only_inside_the_window(tmp_path):
    conn = make_ledger(tmp_path)
    add_invoice(
        conn, vendor="Acme", invoice_number="A", amount_cents=100000, invoice_date="2025-06-01"
    )
    add_invoice(
        conn, vendor="Acme", invoice_number="B", amount_cents=100000, invoice_date="2025-08-01"
    )
    add_invoice(
        conn, vendor="Acme", invoice_number="X", amount_cents=600000, invoice_date="2026-07-01"
    )
    add_invoice(
        conn, vendor="Acme", invoice_number="LATER", amount_cents=100000, invoice_date="2026-08-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = materiality.check(ctx)
    # A is 13 months before X (outside 365d); B is inside; LATER is after X
    assert [f.subject for f in findings] == ["Acme / X / $6,000.00"]
    assert "(1 peer" in findings[0].detail


def test_void_and_cancelled_rows_never_fire_and_never_count_as_peers(tmp_path):
    conn = make_ledger(tmp_path)
    _history(conn)
    add_invoice(
        conn,
        vendor="Acme",
        invoice_number="V",
        amount_cents=1488175,
        invoice_date="2026-07-01",
        status="Void - Duplicate",
    )
    add_invoice(
        conn,
        vendor="Acme",
        invoice_number="C",
        amount_cents=1488175,
        invoice_date="2026-07-02",
        status="Cancelled",
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert materiality.check(ctx) == []


def test_vendor_match_is_case_and_space_insensitive(tmp_path):
    conn = make_ledger(tmp_path)
    _history(conn, vendor="acme ")
    add_invoice(
        conn, vendor="ACME", invoice_number="X", amount_cents=1488175, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert len(materiality.check(ctx)) == 1


def test_thresholds_come_from_config(tmp_path):
    conn = make_ledger(tmp_path)
    _history(conn, amounts=(100000, 100000, 100000))
    add_invoice(
        conn, vendor="Acme", invoice_number="X", amount_cents=250000, invoice_date="2026-07-01"
    )
    ctx = make_context(tmp_path, materiality_floor_cents=200000, materiality_multiple=2.0)
    with ctx.ledger:
        assert len(materiality.check(ctx)) == 1
    ctx = make_context(tmp_path, materiality_enabled=False)
    with ctx.ledger:
        assert materiality.check(ctx) == []


def test_ledger_without_an_ap_book_is_quiet(tmp_path):
    import sqlite3

    tmp_path.mkdir(exist_ok=True)
    sqlite3.connect(tmp_path / "ledger.sqlite3").close()
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert materiality.check(ctx) == []
