"""The auditor's own store: the running checklist it remembers.

SQLite under ``.auditor/<tenant>/`` — a surface the engine never touches
(independence principle 3). This is working memory, not a book of record:
every open item is re-verified from ground truth every night, so the store
can always be rebuilt by running the audit again.

The reconcile pass implements the output contract (docs/auditor-design.md):

- a finding seen again is the same item, silently carried forward;
- a finding whose condition stopped being true resolves itself;
- a resolved finding that returns re-announces as NEW;
- a severity escalation re-announces without resetting first-seen;
- a CRITICAL still open after ``aging_nights`` gets exactly one bump back
  into NEW, never a daily repeat;
- only a lens that actually RAN this night can resolve its items: an open
  finding whose lens crashed or was skipped stays open, unlisted under
  resolved (honesty audit 2026-09-03, 04-F2);
- the reconcile commits only when the caller says so: the runner commits
  after the report is on disk, so a failed write or a dry run never
  consumes a NEW announcement (04-F3);
- the caller may name the announce reason for specific fingerprints
  (``reasons``): a triage snooze that expired announces its item as
  ``snooze-expired`` instead of the generic new/returned (2026-09-04).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .findings import Finding, severity_rank

DB_FILENAME = "auditor.sqlite3"
# The runner's own pseudo-lens (lens-crashed findings); always re-verified.
AUDITOR_LENS = "auditor"

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS findings (
        fingerprint  TEXT NOT NULL,
        tenant       TEXT NOT NULL,
        lens         TEXT NOT NULL,
        subject      TEXT NOT NULL,
        condition    TEXT NOT NULL,
        severity     TEXT NOT NULL,
        detail       TEXT NOT NULL,
        state        TEXT NOT NULL DEFAULT 'open',
        first_seen   TEXT NOT NULL,
        last_seen    TEXT NOT NULL,
        resolved_at  TEXT,
        aging_bumped INTEGER NOT NULL DEFAULT 0,
        reopen_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant, fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auditor_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant      TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running',
        report_path TEXT,
        new_count   INTEGER,
        open_count  INTEGER,
        resolved_count INTEGER,
        error       TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS advisory_state (
        tenant     TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant, key)
    )
    """,
]


@dataclass
class ReconcileResult:
    """The three report sections, in report order."""

    new: list[dict] = field(default_factory=list)  # announce loudly (reason says why)
    open: list[dict] = field(default_factory=list)  # the checklist, oldest first
    resolved: list[dict] = field(default_factory=list)  # closed by reality this run


# Columns added after a store was first created on a machine (2026-09-10:
# reopen_count). CREATE TABLE IF NOT EXISTS leaves an existing table alone,
# so a store that predates the column gets it here, once, idempotently.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("findings", "reopen_count", "INTEGER NOT NULL DEFAULT 0"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _age_nights(first_seen: str, now: str) -> float:
    delta = datetime.fromisoformat(now) - datetime.fromisoformat(first_seen)
    return delta.total_seconds() / 86400


class AuditorStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, root: str | Path) -> AuditorStore:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(root / DB_FILENAME)
        conn.row_factory = sqlite3.Row
        for statement in _SCHEMA:
            conn.execute(statement)
        _ensure_columns(conn)
        conn.commit()
        return cls(conn)

    def __enter__(self) -> AuditorStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ---- the checklist -------------------------------------------------------

    def reconcile(
        self,
        tenant: str,
        findings: list[Finding],
        *,
        now: str,
        aging_nights: int = 3,
        verified_lenses: set[str] | frozenset[str] | None = None,
        commit: bool = True,
        reasons: dict[str, str] | None = None,
    ) -> ReconcileResult:
        """``verified_lenses`` names the lenses that completed this night;
        ``None`` means every lens ran. The ``auditor`` pseudo-lens (crash
        findings) is always verified: the runner itself owns it. With
        ``commit=False`` the writes stay in the open transaction for the
        caller to :meth:`commit` or :meth:`rollback`. ``reasons`` maps a
        fingerprint to the announce reason to use in place of ``new`` /
        ``returned`` when that item is announced tonight; an item merely
        carried forward is never announced, override or not."""
        reasons = reasons or {}
        current = self._dedupe(findings)
        stored = {
            row["fingerprint"]: dict(row)
            for row in self.conn.execute(
                "SELECT * FROM findings WHERE tenant = ?", (tenant,)
            ).fetchall()
        }

        new_items: list[dict] = []
        for fp, finding in current.items():
            row = stored.get(fp)
            if row is None:
                self._insert(tenant, fp, finding, now)
                new_items.append(self._announce(tenant, fp, reasons.get(fp, "new")))
            elif row["state"] == "resolved":
                # It came back: a fresh sighting, first_seen resets, and the
                # return is counted (2026-09-10) so the recurrence lens can see
                # a chore that is cleared by hand and back the next night.
                self.conn.execute(
                    "UPDATE findings SET state='open', severity=?, detail=?, first_seen=?, "
                    "last_seen=?, resolved_at=NULL, aging_bumped=0, "
                    "reopen_count=reopen_count+1 "
                    "WHERE tenant=? AND fingerprint=?",
                    (finding.severity, finding.detail, now, now, tenant, fp),
                )
                new_items.append(self._announce(tenant, fp, reasons.get(fp, "returned")))
            else:
                escalated = severity_rank(finding.severity) > severity_rank(row["severity"])
                self.conn.execute(
                    "UPDATE findings SET severity=?, detail=?, last_seen=? "
                    "WHERE tenant=? AND fingerprint=?",
                    (finding.severity, finding.detail, now, tenant, fp),
                )
                if escalated:
                    new_items.append(self._announce(tenant, fp, "escalated"))
                elif (
                    finding.severity == "CRITICAL"
                    and not row["aging_bumped"]
                    and _age_nights(row["first_seen"], now) >= aging_nights
                ):
                    self.conn.execute(
                        "UPDATE findings SET aging_bumped=1 WHERE tenant=? AND fingerprint=?",
                        (tenant, fp),
                    )
                    new_items.append(self._announce(tenant, fp, "aging"))

        for fp, row in stored.items():
            if row["state"] != "open" or fp in current:
                continue
            if verified_lenses is not None and row["lens"] not in verified_lenses:
                if row["lens"] != AUDITOR_LENS:
                    continue  # nobody looked tonight; it is not fixed, only unseen
            self.conn.execute(
                "UPDATE findings SET state='resolved', resolved_at=? "
                "WHERE tenant=? AND fingerprint=?",
                (now, tenant, fp),
            )
        if commit:
            self.conn.commit()

        open_rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM findings WHERE tenant=? AND state='open' "
                "ORDER BY first_seen, lens, subject",
                (tenant,),
            ).fetchall()
        ]
        resolved_rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM findings WHERE tenant=? AND state='resolved' AND resolved_at=? "
                "ORDER BY lens, subject",
                (tenant, now),
            ).fetchall()
        ]
        return ReconcileResult(new=new_items, open=open_rows, resolved=resolved_rows)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def open_findings(self, tenant: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM findings WHERE tenant=? AND state='open' "
                "ORDER BY first_seen, lens, subject",
                (tenant,),
            ).fetchall()
        ]

    @staticmethod
    def _dedupe(findings: list[Finding]) -> dict[str, Finding]:
        """Within one run, the same fingerprint keeps its worst sighting."""
        current: dict[str, Finding] = {}
        for f in findings:
            held = current.get(f.fingerprint)
            if held is None or severity_rank(f.severity) > severity_rank(held.severity):
                current[f.fingerprint] = f
        return current

    def _insert(self, tenant: str, fp: str, finding: Finding, now: str) -> None:
        self.conn.execute(
            "INSERT INTO findings (fingerprint, tenant, lens, subject, condition, severity, "
            "detail, state, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,'open',?,?)",
            (
                fp,
                tenant,
                finding.lens,
                finding.subject,
                finding.condition,
                finding.severity,
                finding.detail,
                now,
                now,
            ),
        )

    def _announce(self, tenant: str, fp: str, reason: str) -> dict:
        row = dict(
            self.conn.execute(
                "SELECT * FROM findings WHERE tenant=? AND fingerprint=?", (tenant, fp)
            ).fetchone()
        )
        row["reason"] = reason
        return row

    # ---- run bookkeeping (the auditor's own heartbeat source) ---------------

    def start_run(self, tenant: str, *, now: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO auditor_runs (tenant, started_at) VALUES (?, ?)", (tenant, now)
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_state(self, tenant: str, key: str) -> str | None:
        """A remembered advisory value (e.g. the last-rendered 1099 appendix
        fingerprint). Working memory like everything here: losing it costs
        one extra full render, never a fact."""
        row = self.conn.execute(
            "SELECT value FROM advisory_state WHERE tenant = ? AND key = ?", (tenant, key)
        ).fetchone()
        return None if row is None else str(row[0])

    def set_state(self, tenant: str, key: str, value: str, *, now: str) -> None:
        self.conn.execute(
            "INSERT INTO advisory_state (tenant, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tenant, key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (tenant, key, value, now),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        now: str,
        report_path: str = "",
        new_count: int = 0,
        open_count: int = 0,
        resolved_count: int = 0,
        error: str = "",
    ) -> None:
        self.conn.execute(
            "UPDATE auditor_runs SET finished_at=?, status=?, report_path=?, new_count=?, "
            "open_count=?, resolved_count=?, error=? WHERE id=?",
            (now, status, report_path, new_count, open_count, resolved_count, error, run_id),
        )
        self.conn.commit()

    def last_run(self, tenant: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM auditor_runs WHERE tenant=? ORDER BY id DESC LIMIT 1", (tenant,)
        ).fetchone()
        return dict(row) if row else None
