"""Unit tests for ledger migration v7 (``llm_calls``, phase 7 row 7.9).

Every policy-driven model call leaves one row: which job asked, under which
run key, which tier and model answered, what it cost. Purely additive: a
new table and its indexes; no existing table changes. Flagged as a one-way
door in the PR (a ledger schema change), the migration 6 precedent.
"""

from __future__ import annotations

import sqlite3

from core.ledger.migrations import applied_versions, migrate
from core.ledger.schema import MIGRATIONS

REQUIRED_COLUMNS = {
    "id",
    "tenant",
    "job_type",
    "run_key",
    "adapter",
    "model",
    "provider_model",
    "tokens_in",
    "tokens_out",
    "usd",
    "retries",
    "latency_ms",
    "created_at",
}


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def _at_version_6() -> sqlite3.Connection:
    """A ledger as it stood before this row: migrations 1 to 6 applied by
    hand, version 7 unknown to it."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, statements in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version > 6:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'then')", (version,)
        )
    conn.commit()
    return conn


def test_v7_applies_on_a_fresh_ledger_and_is_idempotent():
    conn = _fresh()
    assert {1, 2, 3, 4, 5, 6, 7} <= applied_versions(conn)
    assert migrate(conn) == []


def test_llm_calls_carries_the_telemetry_columns():
    conn = _fresh()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_calls" in tables
    info = {r[1]: (r[2].upper(), r[3]) for r in conn.execute("PRAGMA table_info(llm_calls)")}
    assert set(info) >= REQUIRED_COLUMNS
    # money is a decimal STRING in the ledger, never a float column
    assert info["usd"][0] == "TEXT"
    assert info["tokens_in"][0] == "INTEGER"
    assert info["tokens_out"][0] == "INTEGER"
    assert info["retries"][0] == "INTEGER"


def test_v7_upgrades_a_v6_ledger_in_place_and_keeps_its_rows():
    conn = _at_version_6()
    assert 7 not in applied_versions(conn)
    conn.execute(
        "INSERT INTO job_records (idempotency_key, run_key, tenant, agent, job, "
        "record_type, payload_json, created_at) "
        "VALUES ('k1', 'rk', 'demo', 'a', 'j', 'demo.move', '{}', 'n')"
    )
    conn.commit()
    # 7 is the next version this ledger owes; a later migration rides the
    # same call (8 landed in row 7.23), so read the head, not the whole list.
    assert migrate(conn)[0] == 7
    assert 7 in applied_versions(conn)
    assert conn.execute("SELECT COUNT(*) FROM job_records").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    # the earlier tables are untouched by THIS migration (additive only):
    # read migration 7's own statements rather than the end state, which a
    # later migration in the same call also shaped.
    assert all("job_records" not in statement for statement in dict(MIGRATIONS)[7])
    before = {r[1] for r in _at_version_6().execute("PRAGMA table_info(job_records)")}
    after = {r[1] for r in conn.execute("PRAGMA table_info(job_records)")}
    assert before <= after
