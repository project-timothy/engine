"""Reconcile-unknowns lens evals (2026-09-04): money that left the bank and
matched no ledger row was recorded by the engine as ``ap.reconcile.unknown``
and then never mentioned again (the 2026-05-19 lesson: an unlogged payment,
not a search miss). Each clearing surfaces once, keyed by its bank id."""

from __future__ import annotations

from auditor.findings import Finding
from auditor.lenses import reconcile

from .fixtures import add_event, add_expense_report, make_context, make_ledger


def _unknown(
    conn, qbo_id, *, date, payee="Someone", amount_cents=425000, check_ref="", created_at=None
):
    add_event(
        conn,
        event_type="ap.reconcile.unknown",
        payload={
            "qbo_id": qbo_id,
            "payee": payee,
            "amount_cents": amount_cents,
            "date": date,
            "check_ref": check_ref,
        },
        created_at=created_at or f"{date}T12:00:00+00:00",
    )


def test_recent_unknown_clearing_surfaces_once_by_bank_id(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:285", date="2026-07-15", payee="A Contractor", check_ref="4051")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("Purchase:285", "unknown-clearing", "WARN")
    ]
    assert "$4,250.00 to A Contractor on 2026-07-15 (check 4051)" in findings[0].detail


def test_clearing_older_than_the_window_ages_out(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-06-01")  # 50 days before NOW
    _unknown(conn, "Purchase:2", date="2026-07-01")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:2"]
    ctx = make_context(tmp_path, reconcile_unknown_window_days=60)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:1", "Purchase:2"]


def test_two_events_for_one_clearing_carry_the_newest_amount(tmp_path):
    """Purchase 304 (2026-09-16): the QBO record was trimmed from $5,731.18 to
    $5,689.28 after the first unknown event was written, so the oldest event
    carries a figure that never cleared. The detail line reports the newest
    event; the fingerprint stays subject-only, so the mute already on the item
    survives."""
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:304", date="2026-07-15", payee="A Vendor", amount_cents=573118)
    _unknown(conn, "Purchase:304", date="2026-07-15", payee="A Vendor", amount_cents=568928)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert len(findings) == 1
    assert "$5,689.28" in findings[0].detail
    assert "$5,731.18" not in findings[0].detail
    unchanged = Finding(
        lens="reconcile",
        subject="Purchase:304",
        condition="unknown-clearing",
        severity="WARN",
        detail="the fingerprint ignores the detail line",
    )
    assert findings[0].fingerprint == unchanged.fingerprint


def test_2026_09_17_an_expense_reports_own_purchase_is_not_unknown_money(tmp_path):
    """Purchase 304 — a staffer's September reimbursement, cleared 2026-09-15
    — is the record the ENGINE itself wrote for expense report #2 at ``expenses
    match``. Reconcile had no expense-report tier the night it cleared, so it
    logged ``ap.reconcile.unknown``; the tier landed the next morning (#291,
    issue #281) and correctly re-decides the clearing as already recorded.
    But an event log is append-only and that re-decision writes nothing, so
    the stale unknown stayed the newest word and this lens re-asked the owner
    about his own recorded money every night for the rest of the 30-day
    window — the 7th hand mute of a finding the engine had already answered.

    The lens answers it itself rather than trusting an engine verdict: an
    expense report commits money with no ``ap_invoices`` row, and
    ``qbo_purchase_id`` is the exact id the engine recorded for it. That IS
    the ledger row this clearing matched. Identity, not a heuristic."""
    conn = make_ledger(tmp_path)
    add_expense_report(
        conn, person="A Staffer", month="2026-09", total_cents=568928, qbo_purchase_id="304"
    )
    _unknown(
        conn,
        "Purchase:304",
        date="2026-07-15",
        payee="A Payer LLC",
        amount_cents=573118,
        check_ref="EXP-2",
    )
    _unknown(conn, "Purchase:285", date="2026-07-15", payee="A Contractor", check_ref="4051")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [f.subject for f in findings] == ["Purchase:285"]


def test_only_a_recorded_report_on_the_purchase_entity_is_excluded(tmp_path):
    """The join is entity + exact id, and nothing weaker. A report that never
    reached the accounting system carries no id and excludes nothing; another
    entity sharing the raw id is a different record; and another tenant's
    report is not this tenant's money."""
    conn = make_ledger(tmp_path)
    add_expense_report(conn, person="A Staffer", month="2026-09", total_cents=10000)
    add_expense_report(
        conn, tenant="other", person="Someone", month="2026-09", qbo_purchase_id="900"
    )
    add_expense_report(conn, person="Another Staffer", month="2026-09", qbo_purchase_id="304")
    _unknown(conn, "Purchase:900", date="2026-07-15")
    _unknown(conn, "BillPayment:304", date="2026-07-15")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:900", "BillPayment:304"]


def test_payload_without_a_date_falls_back_to_the_event_time(tmp_path):
    conn = make_ledger(tmp_path)
    add_event(
        conn,
        event_type="ap.reconcile.unknown",
        payload={"qbo_id": "Purchase:9", "payee": "", "amount_cents": 100},
        created_at="2026-07-20T12:00:00+00:00",
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [f.subject for f in findings] == ["Purchase:9"]
    assert "(no payee)" in findings[0].detail


def test_disabled_and_eventless_ledgers_are_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-07-15")
    ctx = make_context(tmp_path, reconcile_enabled=False)
    with ctx.ledger:
        assert reconcile.check(ctx) == []
    import sqlite3

    (tmp_path / "empty").mkdir()
    sqlite3.connect(tmp_path / "empty" / "ledger.sqlite3").close()
    ctx = make_context(tmp_path / "empty")
    with ctx.ledger:
        assert reconcile.check(ctx) == []
