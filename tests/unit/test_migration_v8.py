"""Unit tests for ledger migration v8 (retry columns on ``job_records``,
phase 7 row 7.23).

A job may declare bounded retries with backoff; the runner records each
attempt as a ``job_records`` row of type ``engine.retry`` carrying the
policy, the attempt number, when the next attempt is due, and a link to the
previous attempt's record. Purely additive: five nullable or defaulted
columns and one index on the existing table; no row changes meaning. Flagged
as a one-way door in the PR (a ledger schema change), the migration 6 and 7
precedent.
"""

from __future__ import annotations

import sqlite3

from core.ledger.migrations import applied_versions, migrate
from core.ledger.schema import MIGRATIONS

RETRY_COLUMNS = {"retry_policy", "attempt", "next_attempt_at", "parent_record_id", "retry_state"}


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def _at_version_7() -> sqlite3.Connection:
    """A ledger as it stood before this row: migrations 1 to 7 applied by
    hand, version 8 unknown to it."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, statements in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version > 7:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'then')", (version,)
        )
    conn.commit()
    return conn


def test_v8_applies_on_a_fresh_ledger_and_is_idempotent():
    conn = _fresh()
    assert {1, 2, 3, 4, 5, 6, 7, 8} <= applied_versions(conn)
    assert migrate(conn) == []


def test_job_records_carries_the_retry_columns():
    conn = _fresh()
    info = {
        r[1]: (r[2].upper(), r[3], r[4]) for r in conn.execute("PRAGMA table_info(job_records)")
    }
    assert set(info) >= RETRY_COLUMNS
    assert info["retry_policy"][0] == "TEXT"  # json, NULL on a plain job record
    assert info["attempt"][0] == "INTEGER"
    assert info["next_attempt_at"][0] == "TEXT"  # ISO-8601 UTC, NULL when nothing is due
    assert info["parent_record_id"][0] == "INTEGER"
    assert info["retry_state"][0] == "TEXT"
    # a plain job record (ctx.record_now) reads as "not a retry": nulls and defaults
    conn.execute(
        "INSERT INTO job_records (idempotency_key, run_key, tenant, agent, job, "
        "record_type, payload_json, created_at) "
        "VALUES ('k1', 'rk', 'demo', 'a', 'j', 'demo.move', '{}', 'n')"
    )
    row = conn.execute(
        "SELECT retry_policy, attempt, next_attempt_at, parent_record_id, retry_state "
        "FROM job_records WHERE idempotency_key = 'k1'"
    ).fetchone()
    assert row == (None, 0, None, None, "")


def test_v8_upgrades_a_v7_ledger_in_place_and_keeps_its_rows():
    conn = _at_version_7()
    assert 8 not in applied_versions(conn)
    conn.execute(
        "INSERT INTO job_records (idempotency_key, run_key, tenant, agent, job, "
        "record_type, payload_json, created_at) "
        "VALUES ('k1', 'rk', 'demo', 'a', 'j', 'demo.move', '{}', 'n')"
    )
    conn.execute(
        "INSERT INTO llm_calls (tenant, job_type, adapter, model, created_at) "
        "VALUES ('demo', 'x', 'fixture', 'm', 'n')"
    )
    conn.commit()
    assert migrate(conn) == [8]
    assert 8 in applied_versions(conn)
    assert conn.execute("SELECT COUNT(*) FROM job_records").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    before = {r[1] for r in _at_version_7().execute("PRAGMA table_info(job_records)")}
    after = {r[1] for r in conn.execute("PRAGMA table_info(job_records)")}
    assert after == before | RETRY_COLUMNS, "additive only: the old columns all survive"
    # every other table is untouched by the migration
    for table in ("runs", "events", "approval_queue", "ap_invoices", "expense_report", "llm_calls"):
        old = [r[1] for r in _at_version_7().execute(f"PRAGMA table_info({table})")]
        new = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert old == new, table
