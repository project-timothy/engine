"""Forward-only migration runner.

Applies any migration whose version is not yet recorded in
``schema_migrations``. Idempotent: running it against an up-to-date database
is a no-op, so it is safe to call on every ``Ledger.open``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .schema import MIGRATIONS


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Return the versions newly applied."""
    _ensure_migrations_table(conn)
    done = applied_versions(conn)
    newly_applied: list[int] = []
    for version, statements in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version in done:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        newly_applied.append(version)
    return newly_applied
