"""The auditor store counts returns (2026-09-10, docs/decisions/
2026-09-10-recurrence-reopen-count.md): a resolved finding that comes back
increments ``reopen_count`` and the column is added to a store that
predates it, idempotently, on open."""

from __future__ import annotations

import sqlite3

from auditor.findings import Finding
from auditor.store import DB_FILENAME, AuditorStore


def _f(subject="internal disk"):
    return Finding(lens="host", subject=subject, condition="low-free", severity="WARN", detail="d")


def _count(store, subject="internal disk") -> int:
    row = store.conn.execute(
        "SELECT reopen_count, state FROM findings WHERE subject = ?", (subject,)
    ).fetchone()
    return int(row["reopen_count"])


def test_a_return_increments_the_reopen_count(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f()], now="2026-09-01T02:00:00+00:00")
        assert _count(store) == 0
        store.reconcile("t", [], now="2026-09-02T02:00:00+00:00")  # cleared by hand
        store.reconcile("t", [_f()], now="2026-09-03T02:00:00+00:00")  # back
        assert _count(store) == 1
        store.reconcile("t", [_f()], now="2026-09-04T02:00:00+00:00")  # still open: no bump
        assert _count(store) == 1
        store.reconcile("t", [], now="2026-09-05T02:00:00+00:00")
        store.reconcile("t", [_f()], now="2026-09-06T02:00:00+00:00")
        assert _count(store) == 2


def test_an_older_store_gains_the_column_on_open(tmp_path):
    """A store created before the column exists (the live Mini's) must open
    and count without a rebuild; opening twice must not fail on a
    duplicate column."""
    conn = sqlite3.connect(tmp_path / DB_FILENAME)
    conn.execute(
        """
        CREATE TABLE findings (
            fingerprint TEXT NOT NULL, tenant TEXT NOT NULL, lens TEXT NOT NULL,
            subject TEXT NOT NULL, condition TEXT NOT NULL, severity TEXT NOT NULL,
            detail TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, resolved_at TEXT,
            aging_bumped INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tenant, fingerprint)
        )
        """
    )
    conn.execute(
        "INSERT INTO findings VALUES (?, 't', 'host', 'internal disk', 'low-free', 'WARN', "
        "'d', 'resolved', '2026-08-30T02:00:00+00:00', '2026-08-30T02:00:00+00:00', "
        "'2026-08-31T02:00:00+00:00', 0)",
        (_f().fingerprint,),
    )
    conn.commit()
    conn.close()
    with AuditorStore.open(tmp_path) as store:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(findings)").fetchall()}
        assert "reopen_count" in cols
        assert (
            store.conn.execute("SELECT reopen_count FROM findings").fetchone()["reopen_count"] == 0
        )
    with AuditorStore.open(tmp_path) as store:  # second open: idempotent
        store.reconcile("t", [_f()], now="2026-09-01T02:00:00+00:00")
        assert _count(store) == 1, "the pre-existing resolved row's return is counted"
