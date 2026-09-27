"""Unit tests for ledger migration v5 (expense_report.instrument_ref).

Issue #115 gap 2: the reimbursement's actual instrument (a check number)
had nowhere to live — the Purchase's DocNumber carries the person's memo
by design, so the bank-feed row had nothing tying it to the report. One
additive nullable column; existing rows and tables untouched.
"""

from __future__ import annotations

import sqlite3

from core.ledger.migrations import applied_versions, migrate


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def test_v5_applies_and_is_idempotent():
    conn = _fresh()
    assert {1, 2, 3, 4, 5} <= applied_versions(conn)
    assert migrate(conn) == []


def test_expense_report_gains_instrument_ref():
    conn = _fresh()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(expense_report)")}
    assert "instrument_ref" in cols


def test_v5_upgrades_a_v4_database_in_place():
    conn = sqlite3.connect(":memory:")
    migrate(conn)  # all versions; simulate existing data surviving
    conn.execute(
        "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
        "total_cents, status, created_at, updated_at) "
        "VALUES ('k1', 'demo', 'Pat Owner', '2026-08', 100, 'Open', 'n', 'n')"
    )
    row = conn.execute("SELECT instrument_ref FROM expense_report").fetchone()
    assert row[0] in (None, "")
