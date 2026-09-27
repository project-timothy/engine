"""Tripwire: the auditor's fixture DDL must match the engine's real schema.

The independence lint forbids importing core under ``auditor/``, so the lens
evals build ledgers from a deliberate DDL copy in ``auditor/evals/fixtures``.
This test lives on the ENGINE side of the boundary, where importing both is
allowed, and fails the suite the moment a schema migration lands without the
auditor's copy (and therefore its SQL) being consciously revisited.
"""

from __future__ import annotations

import sqlite3

from auditor.evals.fixtures import make_ledger
from core.ledger import Ledger

TABLES = [
    "runs",
    "events",
    "approval_queue",
    "ap_invoices",
    "ap_status_history",
    "expense_report",
    "expense_line",
]


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    # (name, type, notnull, default) — pk order intentionally included via cid order
    return [(r[1], r[2].upper(), r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")]


def test_fixture_schema_matches_engine_schema(tmp_path):
    with Ledger.open(tmp_path / "engine") as ledger:
        engine_conn = ledger.conn
        fixture_conn = make_ledger(tmp_path / "fixture")
        for table in TABLES:
            engine_cols = _columns(engine_conn, table)
            fixture_cols = _columns(fixture_conn, table)
            assert fixture_cols == engine_cols, (
                f"auditor fixture DDL for {table!r} drifted from the engine schema; "
                "update auditor/evals/fixtures.py AND re-check every lens SQL that "
                "touches this table"
            )
        fixture_conn.close()
