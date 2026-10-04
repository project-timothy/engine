"""Check-number sequence-gap evals (old A-BR-005, re-founded on the ledger
2026-09-04). A hand-written check that never touched the ledger leaves a
hole in the paper series; the bank's own sightings (``ap.reconcile.unknown``
events) count as observed so a cleared-but-unrecorded number is not a gap
twice (the reconcile lens already names it)."""

from __future__ import annotations

from auditor.lenses import check_gaps

from .fixtures import NOW, add_event, add_expense_report, add_invoice, make_context, make_ledger

# NOW is 2026-07-21; the 90-day window opens 2026-04-22.


def _paid(conn, ref, date, **kw):
    return add_invoice(
        conn, invoice_number=f"inv-{ref}", status="Paid", payment_date=date, check_ref=ref, **kw
    )


def test_gap_inside_the_window_names_the_missing_numbers(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "Check 3045", "2026-06-03")
    _paid(conn, "Check 3046", "2026-06-03")
    _paid(conn, "Check 3049", "2026-07-17")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = check_gaps.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("3xxx 3047-3048", "check-gap", "WARN")
    ]
    assert "3047, 3048" in findings[0].detail
    assert "3046 (2026-06-03)" in findings[0].detail
    assert "3049 (2026-07-17)" in findings[0].detail


def test_gap_older_than_the_window_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "Check 3017", "2026-01-22")
    _paid(conn, "Check 3018", "2026-02-06")
    _paid(conn, "Check 3020", "2026-02-12")  # 3019 missing, but February
    _paid(conn, "Check 3021", "2026-07-01")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert check_gaps.check(ctx) == []


def test_series_with_too_few_numbers_is_not_a_sequence(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "Check 3045", "2026-06-03")
    _paid(conn, "Check 3049", "2026-07-17")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert check_gaps.check(ctx) == []


def test_number_seen_by_the_bank_is_observed_not_a_gap(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "Check 3045", "2026-06-03")
    _paid(conn, "Check 3046", "2026-06-03")
    _paid(conn, "Check 3049", "2026-07-17")
    add_event(
        conn,
        event_type="ap.reconcile.unknown",
        payload={
            "qbo_id": "Purchase:1",
            "check_ref": "3047",
            "date": "2026-07-01",
            "amount_cents": 1,
        },
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = check_gaps.check(ctx)
    assert [f.subject for f in findings] == ["3xxx 3048"]


def test_reimbursement_instrument_counts_as_observed(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "Check 3045", "2026-06-03")
    _paid(conn, "Check 3046", "2026-06-03")
    _paid(conn, "Check 3049", "2026-07-17")
    add_expense_report(
        conn, status="Reimbursed", instrument_ref="3048", reimbursed_date="2026-07-10"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = check_gaps.check(ctx)
    assert [f.subject for f in findings] == ["3xxx 3047"]


def test_messy_refs_and_compound_cells_yield_their_numbers(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "CK3036", "2026-06-01")
    _paid(conn, "Checks 3037 + 3038", "2026-06-10")
    _paid(conn, "3039+3040 (Jun 20)", "2026-06-20")
    _paid(conn, "Check 3042", "2026-07-01")
    _paid(conn, "BANK ACH X0000", "2026-07-02")  # a 7-digit id never reads as a check
    _paid(conn, "Vendor id 0000930", "2026-07-03")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = check_gaps.check(ctx)
    assert [f.subject for f in findings] == ["3xxx 3041"]


def test_series_filter_restricts_to_the_paper_series(tmp_path):
    conn = make_ledger(tmp_path)
    for n in (7051, 7052, 7055):
        _paid(conn, f"Check {n}", "2026-07-01")
    for n in (3045, 3046, 3049):
        _paid(conn, f"Check {n}", "2026-07-01")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert sorted(f.subject for f in check_gaps.check(ctx)) == [
            "3xxx 3047-3048",
            "7xxx 7053-7054",
        ]
    ctx = make_context(tmp_path, check_gaps_series=["3xxx"])
    with ctx.ledger:
        assert [f.subject for f in check_gaps.check(ctx)] == ["3xxx 3047-3048"]


def test_unsettled_rows_do_not_count(tmp_path):
    conn = make_ledger(tmp_path)
    _paid(conn, "Check 3045", "2026-06-03")
    _paid(conn, "Check 3046", "2026-06-03")
    add_invoice(conn, invoice_number="sched", status="Scheduled", check_ref="3049")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert check_gaps.check(ctx) == []


def test_disabled_and_bookless_ledgers_are_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    for n in (3045, 3046, 3049):
        _paid(conn, f"Check {n}", "2026-07-01")
    ctx = make_context(tmp_path, check_gaps_enabled=False)
    with ctx.ledger:
        assert check_gaps.check(ctx) == []
    import sqlite3

    (tmp_path / "empty").mkdir()
    sqlite3.connect(tmp_path / "empty" / "ledger.sqlite3").close()
    ctx = make_context(tmp_path / "empty", now=NOW)
    with ctx.ledger:
        assert check_gaps.check(ctx) == []
