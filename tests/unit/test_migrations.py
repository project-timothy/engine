"""Unit tests for the forward-only migration runner."""

from __future__ import annotations

import sqlite3

from core.ledger.migrations import applied_versions, migrate
from core.ledger.schema import MIGRATIONS

ALL_VERSIONS = sorted(version for version, _ in MIGRATIONS)


def test_migrate_applies_all_pending_then_is_noop():
    conn = sqlite3.connect(":memory:")
    applied = migrate(conn)
    assert applied == ALL_VERSIONS
    assert applied_versions(conn) == set(ALL_VERSIONS)

    # Running again applies nothing.
    assert migrate(conn) == []
    assert applied_versions(conn) == set(ALL_VERSIONS)


def test_migrate_creates_expected_tables():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "events", "approval_queue", "schema_migrations"} <= tables
