"""Read-only access to the engine's ledger, with the auditor's own SQL.

Independence principle 1: recompute, never reread conclusions. This module
gives lenses raw rows and raw event records; every derivation happens in the
lens. Principle 3 is enforced at the connection: SQLite is opened ``mode=ro``,
so a write attempt is an error, not a hazard.

The ledger location follows the ENGINE's convention ($ENGINE_LEDGER_ROOT or
./.ledger, then <slug>/) — a documented coordination-by-convention point:
the auditor must find the same ledger the engine writes, without importing
the engine's resolver.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DB_FILENAME = "ledger.sqlite3"
EVENT_LOG_FILENAME = "event_log.jsonl"


def resolve_ledger_root(slug: str, ledger_dir: str | None) -> Path:
    base = ledger_dir or os.environ.get("ENGINE_LEDGER_ROOT") or ".ledger"
    return Path(base).expanduser() / slug


class LedgerReader:
    def __init__(self, root: Path, conn: sqlite3.Connection) -> None:
        self.root = root
        self.conn = conn

    @classmethod
    def open(cls, root: str | Path) -> LedgerReader:
        root = Path(root)
        db = root / DB_FILENAME
        if not db.exists():
            raise FileNotFoundError(f"no ledger database at {db}")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return cls(root, conn)

    def __enter__(self) -> LedgerReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def events(self) -> list[dict]:
        """Every event-log record, parsed. Missing log reads as empty."""
        path = self.root / EVENT_LOG_FILENAME
        if not path.exists():
            return []
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records
