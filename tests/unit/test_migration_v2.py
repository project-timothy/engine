"""Unit tests for ledger migration v2 (AP tables)."""

from __future__ import annotations

import sqlite3

from core.ledger.migrations import applied_versions, migrate


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def test_v2_applies_and_is_idempotent():
    conn = _fresh()
    assert {1, 2} <= applied_versions(conn)
    assert migrate(conn) == []


def test_ap_tables_exist_with_idempotency_and_shadow():
    conn = _fresh()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ap_invoices)")}
    assert {
        "idempotency_key",
        "tenant",
        "vendor",
        "invoice_number",
        "amount_cents",
        "status",
        "shadow",
        "check_ref",
        "source_md5",
    } <= cols
    hist = {r[1] for r in conn.execute("PRAGMA table_info(ap_status_history)")}
    assert {"idempotency_key", "invoice_id", "status_from", "status_to"} <= hist


def test_amount_is_integer_cents_not_float():
    # Money is never stored as float: the column is INTEGER cents.
    conn = _fresh()
    decl = {r[1]: (r[2] or "").upper() for r in conn.execute("PRAGMA table_info(ap_invoices)")}
    assert decl["amount_cents"] == "INTEGER"


def test_idempotency_key_is_unique_on_ap_invoices():
    conn = _fresh()
    conn.execute(
        "INSERT INTO ap_invoices (idempotency_key, tenant, vendor, invoice_number,"
        " amount_cents, status, shadow, created_at, updated_at)"
        " VALUES ('k', 't', 'V', '1', 100, 'Received', 1, 'now', 'now')"
    )
    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ap_invoices (idempotency_key, tenant, vendor, invoice_number,"
            " amount_cents, status, shadow, created_at, updated_at)"
            " VALUES ('k', 't', 'V', '1', 100, 'Received', 1, 'now', 'now')"
        )
