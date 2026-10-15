"""Unit tests for ledger migration v9 (a foreign receipt's line, alpha step 3;
the owner's yes of 2026-10-13).

An expense line keeps the amount and currency as the person said them and
the rate that set its dollar value: units per dollar, the receipt's own
date, and the feed it came from. Purely additive: five defaulted columns on
the existing table, and a dollar line reads '' in each. Flagged as a
one-way door in the PR (a ledger schema change), the migration 5 precedent.
"""

from __future__ import annotations

import sqlite3

from core.ledger.migrations import applied_versions, migrate
from core.ledger.schema import MIGRATIONS

FX_COLUMNS = {"said_amount", "said_currency", "fx_rate", "fx_date", "fx_source"}


def _at_version_8() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, statements in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version > 8:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'then')", (version,)
        )
    conn.commit()
    return conn


def test_v9_applies_on_a_fresh_ledger_and_is_idempotent():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    assert 9 in applied_versions(conn)
    assert migrate(conn) == []


def test_a_dollar_line_already_filed_reads_empty_in_every_new_column():
    conn = _at_version_8()
    conn.execute(
        "INSERT INTO expense_report (idempotency_key, tenant, person, month, total_cents, "
        "created_at, updated_at) VALUES ('r', 't', 'Ruth Hollis', '2026-09', 4300, 'then', 'then')"
    )
    conn.execute(
        "INSERT INTO expense_line (idempotency_key, report_id, tenant, amount_cents, created_at) "
        "VALUES ('l', 1, 't', 4300, 'then')"
    )
    conn.commit()
    assert migrate(conn) == [9]
    conn.row_factory = sqlite3.Row
    line = conn.execute("SELECT * FROM expense_line").fetchone()
    assert {k: line[k] for k in FX_COLUMNS} == dict.fromkeys(FX_COLUMNS, "")
    assert line["amount_cents"] == 4300
