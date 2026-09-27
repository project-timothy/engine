"""Unit tests for ledger migration v4 (the expenses agent expense tables).

The two-table schema addition was the design's flagged one-way door
(docs/expenses-design.md §3); the owner approved it 2026-08-04. Existing
tables are untouched — the migration is purely additive.
"""

from __future__ import annotations

import sqlite3

import pytest

from core.ledger.migrations import applied_versions, migrate


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def test_v4_applies_and_is_idempotent():
    conn = _fresh()
    assert {1, 2, 3, 4} <= applied_versions(conn)
    assert migrate(conn) == []


def test_expense_tables_exist_with_required_columns():
    conn = _fresh()
    report = {r[1] for r in conn.execute("PRAGMA table_info(expense_report)")}
    assert {
        "idempotency_key",
        "tenant",
        "person",
        "month",
        "total_cents",
        "status",
        "report_path",
        "manifest_path",
        "qbo_purchase_id",
        "reimbursed_channel",
        "reimbursed_date",
        "cleared_date",
        "shadow",
        "created_at",
        "updated_at",
    } <= report
    line = {r[1] for r in conn.execute("PRAGMA table_info(expense_line)")}
    assert {
        "idempotency_key",
        "report_id",
        "tenant",
        "receipt_file",
        "receipt_sha256",
        "vendor",
        "expense_date",
        "amount_cents",
        "category",
        "project",
        "note",
        "created_at",
    } <= line


def test_expense_money_is_integer_cents_never_float():
    conn = _fresh()
    report = {r[1]: (r[2] or "").upper() for r in conn.execute("PRAGMA table_info(expense_report)")}
    line = {r[1]: (r[2] or "").upper() for r in conn.execute("PRAGMA table_info(expense_line)")}
    assert report["total_cents"] == "INTEGER"
    assert line["amount_cents"] == "INTEGER"


def test_expense_report_status_defaults_open():
    conn = _fresh()
    conn.execute(
        "INSERT INTO expense_report (idempotency_key, tenant, person, month,"
        " total_cents, created_at, updated_at)"
        " VALUES ('k', 't', 'P', '2026-07', 74701, 'now', 'now')"
    )
    status = conn.execute("SELECT status FROM expense_report WHERE idempotency_key='k'").fetchone()
    assert status[0] == "Open"


def test_idempotency_keys_are_unique_on_both_tables():
    conn = _fresh()
    conn.execute(
        "INSERT INTO expense_report (idempotency_key, tenant, person, month,"
        " total_cents, created_at, updated_at)"
        " VALUES ('r', 't', 'P', '2026-07', 100, 'now', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month,"
            " total_cents, created_at, updated_at)"
            " VALUES ('r', 't', 'P', '2026-07', 100, 'now', 'now')"
        )
    conn.execute(
        "INSERT INTO expense_line (idempotency_key, report_id, tenant, receipt_file,"
        " receipt_sha256, amount_cents, created_at)"
        " VALUES ('l', 1, 't', 'f.pdf', 'sha', 100, 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO expense_line (idempotency_key, report_id, tenant, receipt_file,"
            " receipt_sha256, amount_cents, created_at)"
            " VALUES ('l', 1, 't', 'f.pdf', 'sha', 100, 'now')"
        )


def test_existing_tables_untouched_by_v4():
    conn = _fresh()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ap_invoices)")}
    assert {"idempotency_key", "vendor", "amount_cents", "qbo_bill_id", "qbo_payment_id"} <= cols
