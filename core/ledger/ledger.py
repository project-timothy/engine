"""The Ledger: the engine's system of record.

One ``Ledger`` is one per-deployment git repository holding a SQLite database
(queryable state), an append-only JSONL event log (human-diffable history),
and whatever TOML registries later phases add. The code repository and the
ledger repository are deliberately separate: code ships as ``core/`` + a PR,
data lives in its own git-backed deployment directory.

Idempotency (invariant 3) is enforced here, not in callers. ``record_run``,
``append_event``, and ``enqueue_approval`` all take an idempotency key and use
``INSERT OR IGNORE``; a repeated key is a no-op and the method reports it as
such (``is_new`` / a boolean return). The runner uses ``find_run`` to
short-circuit an entire re-run before any side effect happens.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .event_log import append_event_line, read_event_lines, repair_event_log
from .git_commit import commit_all, ensure_repo, structured_message
from .migrations import migrate

DB_FILENAME = "ledger.sqlite3"


class IdempotencyKeyConflict(RuntimeError):
    """The same idempotency key was reused for a different tenant/agent/job.

    Invariant 3 makes a repeated key an idempotent replay of the SAME job.
    A key shared across different jobs is a caller bug; silently returning
    the other job's row would corrupt run history, so it raises instead.
    """


@dataclass(frozen=True)
class StoredRun:
    """A row from the ``runs`` table, plus whether this call created it."""

    id: int
    idempotency_key: str
    tenant: str
    agent: str
    job: str
    status: str
    shadow: bool
    result_json: str
    summary: str
    created_at: str
    is_new: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    """Git-backed SQLite + JSONL ledger for one deployment."""

    def __init__(self, root: Path, conn: sqlite3.Connection) -> None:
        self._root = root
        self._conn = conn
        self._in_txn = False
        self._pending_jsonl: list[dict[str, Any]] = []

    @classmethod
    def open(cls, root: str | Path, *, repair_event_log: bool = False) -> Ledger:
        """Open the ledger. ``repair_event_log=True`` (the runner, under its
        run lock) converges the JSONL log onto SQLite's events table —
        truncating a torn trailing fragment and backfilling missing tail
        lines (#136). Readers that hold no lock (the auditor) leave the
        file alone; read_event_lines tolerates the torn tail meanwhile."""
        root = Path(root)
        ensure_repo(root)
        conn = sqlite3.connect(str(root / DB_FILENAME))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
        ledger = cls(root, conn)
        if repair_event_log:
            ledger._repair_event_log()
        return ledger

    def _repair_event_log(self) -> None:
        rows = self._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        repair_event_log(
            self._root,
            [
                {
                    "idempotency_key": r["idempotency_key"],
                    "run_id": r["run_id"],
                    "tenant": r["tenant"],
                    "agent": r["agent"],
                    "event_type": r["event_type"],
                    "payload": json.loads(r["payload_json"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        )

    def atomic(self):
        """All writes inside commit together or not at all (#136).

        The runner persists run + events + approvals as one unit: a crash
        between them must never leave a recorded run whose cards were never
        created (that run would replay forever). JSONL lines queue up and
        flush only AFTER the SQLite commit, so a rolled-back transaction
        leaves no phantom lines; a crash between commit and flush is the
        backfill case _repair_event_log heals on the next locked open.
        """
        from contextlib import contextmanager

        @contextmanager
        def _txn():
            self._in_txn = True
            self._pending_jsonl = []
            try:
                yield self
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
                for record in self._pending_jsonl:
                    append_event_line(self._root, record)
            finally:
                self._in_txn = False
                self._pending_jsonl = []

        return _txn()

    # -- properties --------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def db_path(self) -> Path:
        return self._root / DB_FILENAME

    @property
    def conn(self) -> sqlite3.Connection:
        """The underlying connection, for domain store modules.

        Domain tables (ap_invoices, ...) get their own store modules rather
        than methods here; this property is their sanctioned access path.
        """
        return self._conn

    # -- runs --------------------------------------------------------------

    def find_run(self, idempotency_key: str) -> StoredRun | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return self._row_to_run(row, is_new=False) if row else None

    def record_run(
        self,
        *,
        idempotency_key: str,
        tenant: str,
        agent: str,
        job: str,
        status: str,
        shadow: bool,
        result_json: str,
        summary: str = "",
    ) -> StoredRun:
        """Insert a run row, or return the existing one if the key is known.

        The returned ``StoredRun.is_new`` tells the caller whether this was a
        fresh write (True) or an idempotent hit on a prior run (False).
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO runs
                (idempotency_key, tenant, agent, job, status, shadow,
                 result_json, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                tenant,
                agent,
                job,
                status,
                1 if shadow else 0,
                result_json,
                summary,
                _now(),
            ),
        )
        if not self._in_txn:
            self._conn.commit()
        is_new = cur.rowcount == 1
        row = self._conn.execute(
            "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        stored = self._row_to_run(row, is_new=is_new)
        if not is_new and (stored.tenant, stored.agent, stored.job) != (tenant, agent, job):
            raise IdempotencyKeyConflict(
                f"idempotency key {idempotency_key!r} already belongs to "
                f"{stored.tenant}/{stored.agent}/{stored.job}, refusing reuse for "
                f"{tenant}/{agent}/{job}"
            )
        return stored

    # -- events ------------------------------------------------------------

    def append_event(
        self,
        *,
        idempotency_key: str,
        run_id: int,
        tenant: str,
        agent: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Append an event to SQLite and the JSONL log. Returns True if new.

        The JSONL line is written only when the SQLite insert is new, so the
        append-only log never grows on an idempotent re-run.
        """
        created_at = _now()
        payload_json = json.dumps(payload, sort_keys=True)
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO events
                (idempotency_key, run_id, tenant, agent, event_type,
                 payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (idempotency_key, run_id, tenant, agent, event_type, payload_json, created_at),
        )
        if not self._in_txn:
            self._conn.commit()
        is_new = cur.rowcount == 1
        if is_new:
            record = {
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "tenant": tenant,
                "agent": agent,
                "event_type": event_type,
                "payload": payload,
                "created_at": created_at,
            }
            if self._in_txn:
                # flushes after the commit; a rollback drops it (#136)
                self._pending_jsonl.append(record)
            else:
                append_event_line(self._root, record)
        return is_new

    # ---- job records: durable at job time (honesty audit #170, 03-F9) ----

    def record_now(
        self,
        *,
        idempotency_key: str,
        run_key: str,
        tenant: str,
        agent: str,
        job: str,
        record_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Write a job record and commit it AT ONCE (unless a transaction is
        already open, in which case it rides that transaction). Returns True
        if new. This is the record half of record-then-move: a job writes
        the record, then moves the file, so a death between the two leaves
        the record, never a silent move. Idempotent on the key."""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO job_records
                (idempotency_key, run_key, run_id, tenant, agent, job,
                 record_type, payload_json, created_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                run_key,
                tenant,
                agent,
                job,
                record_type,
                json.dumps(payload, sort_keys=True),
                _now(),
            ),
        )
        if not self._in_txn:
            self._conn.commit()
        return cur.rowcount == 1

    def job_records(
        self, *, tenant: str, record_type: str | None = None, run_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Job records for a tenant, newest last; filter by type and/or run key."""
        sql = "SELECT * FROM job_records WHERE tenant = ?"
        args: list[Any] = [tenant]
        if record_type is not None:
            sql += " AND record_type = ?"
            args.append(record_type)
        if run_key is not None:
            sql += " AND run_key = ?"
            args.append(run_key)
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [self._record_to_dict(r) for r in rows]

    def link_job_records(self, *, run_key: str, run_id: int) -> int:
        """Attach every still-unlinked record of ``run_key`` to the run row
        that landed (ok or FAILED). Returns the number linked."""
        cur = self._conn.execute(
            "UPDATE job_records SET run_id = ? WHERE run_key = ? AND run_id IS NULL",
            (run_id, run_key),
        )
        if not self._in_txn:
            self._conn.commit()
        return cur.rowcount

    # ---- retry records: cross-run retries (phase 7 row 7.23) ----

    def record_retry(
        self,
        *,
        idempotency_key: str,
        run_key: str,
        run_id: int | None,
        tenant: str,
        agent: str,
        job: str,
        record_type: str,
        payload: dict[str, Any],
        retry_policy: str,
        attempt: int,
        delay_seconds: int | None,
        parent_record_id: int | None,
        retry_state: str,
    ) -> int | None:
        """Write one attempt's retry record (a ``job_records`` row carrying
        the migration 8 columns). ``delay_seconds`` sets ``next_attempt_at``
        that many seconds after the row's own ``created_at`` (one clock
        reading for both); None leaves nothing due. Idempotent on the key:
        returns the row id when new, None when the attempt was already on
        the record."""
        created = datetime.now(UTC)
        next_attempt_at = (
            (created + timedelta(seconds=delay_seconds)).isoformat()
            if delay_seconds is not None
            else None
        )
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO job_records
                (idempotency_key, run_key, run_id, tenant, agent, job,
                 record_type, payload_json, created_at,
                 retry_policy, attempt, next_attempt_at, parent_record_id, retry_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                run_key,
                run_id,
                tenant,
                agent,
                job,
                record_type,
                json.dumps(payload, sort_keys=True),
                created.isoformat(),
                retry_policy,
                attempt,
                next_attempt_at,
                parent_record_id,
                retry_state,
            ),
        )
        if not self._in_txn:
            self._conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else None

    def retry_record(self, record_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM job_records WHERE id = ?", (record_id,)).fetchone()
        return self._record_to_dict(row) if row else None

    def scheduled_retry(self, *, run_key: str) -> dict[str, Any] | None:
        """The one pending attempt under ``run_key``, if any (a hand re-run
        joins the chain instead of forking a second one)."""
        row = self._conn.execute(
            "SELECT * FROM job_records WHERE run_key = ? AND retry_state = 'scheduled' "
            "ORDER BY id DESC LIMIT 1",
            (run_key,),
        ).fetchone()
        return self._record_to_dict(row) if row else None

    def due_retries(self, tenant: str, *, as_of: str | None) -> list[dict[str, Any]]:
        """Scheduled retries whose ``next_attempt_at`` has passed ``as_of``
        (an ISO-8601 UTC stamp); ``None`` means every scheduled retry, due
        or not (the owner's ``--now``). Oldest due first."""
        sql = "SELECT * FROM job_records WHERE tenant = ? AND retry_state = 'scheduled'"
        args: list[Any] = [tenant]
        if as_of is not None:
            sql += " AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?"
            args.append(as_of)
        rows = self._conn.execute(sql + " ORDER BY next_attempt_at, id", args).fetchall()
        return [self._record_to_dict(r) for r in rows]

    def set_retry_state(self, record_id: int, state: str, *, expect: str | None = None) -> bool:
        """Move a retry record to ``state``; with ``expect`` only from that
        state (the compare-and-set a resume uses so two processes never
        both take the same attempt). Commits at once unless a transaction
        is open. Returns whether the row changed."""
        if expect is None:
            cur = self._conn.execute(
                "UPDATE job_records SET retry_state = ? WHERE id = ?", (state, record_id)
            )
        else:
            cur = self._conn.execute(
                "UPDATE job_records SET retry_state = ? WHERE id = ? AND retry_state = ?",
                (state, record_id, expect),
            )
        if not self._in_txn:
            self._conn.commit()
        return cur.rowcount == 1

    @staticmethod
    def _record_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def events_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            {
                "idempotency_key": r["idempotency_key"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]

    def read_event_log(self) -> list[dict[str, Any]]:
        return read_event_lines(self._root)

    # -- approval queue ----------------------------------------------------

    def enqueue_approval(
        self,
        *,
        idempotency_key: str,
        run_id: int,
        tenant: str,
        agent: str,
        action_type: str,
        params: dict[str, Any],
    ) -> bool:
        """Enqueue an action for human approval. Returns True if newly queued.

        Every external send or money movement passes through here instead of
        executing (invariant 7). Phase 1 builds the table and the write path;
        the interactive review tooling is Phase 2+.
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO approval_queue
                (idempotency_key, run_id, tenant, agent, action_type,
                 params_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                idempotency_key,
                run_id,
                tenant,
                agent,
                action_type,
                json.dumps(params, sort_keys=True),
                _now(),
            ),
        )
        if not self._in_txn:
            self._conn.commit()
        return cur.rowcount == 1

    def list_approvals(self, tenant: str, *, status: str | None = None) -> list[dict[str, Any]]:
        """Approval-queue rows for a tenant, optionally filtered by status."""
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM approval_queue WHERE tenant = ? ORDER BY id", (tenant,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM approval_queue WHERE tenant = ? AND status = ? ORDER BY id",
                (tenant, status),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "idempotency_key": r["idempotency_key"],
                "agent": r["agent"],
                "action_type": r["action_type"],
                "params": json.loads(r["params_json"]),
                "status": r["status"],
                "created_at": r["created_at"],
                "resolved_at": r["resolved_at"],
            }
            for r in rows
        ]

    def resolve_approval(
        self,
        tenant: str,
        approval_id: int,
        decision: str,
        *,
        param_overrides: dict[str, str] | None = None,
        check: Callable[[str, str, dict[str, Any]], str | None] | None = None,
    ) -> dict[str, Any]:
        """Mark a pending approval approved/rejected. Raises if not pending.

        Resolution only records the decision; nothing executes here. Execution
        with the recorded parameters is the owning job's next run.

        ``param_overrides`` lets the owner correct a card's facts at approval
        time (issue #115: the reimbursement went by check 3050, not the
        proposed default channel). Overrides merge into the stored params so
        every downstream consumer reads the corrected values; the ledger's
        git commit records the change.

        ``check(agent, action_type, merged_params)`` runs BEFORE anything is
        written and refuses the resolution (``ValueError`` carrying its
        message) when a card needs a fact the owner did not supply (phase 7
        row 7.1). A refused card stays pending with its params untouched:
        never approved-but-unexecutable.
        """
        if decision not in ("approved", "rejected"):
            raise ValueError(f"decision must be approved|rejected, got {decision!r}")
        row = self._conn.execute(
            "SELECT * FROM approval_queue WHERE tenant = ? AND id = ?", (tenant, approval_id)
        ).fetchone()
        if row is None:
            raise LookupError(f"no approval #{approval_id} for tenant {tenant!r}")
        if row["status"] != "pending":
            raise ValueError(f"approval #{approval_id} is already {row['status']}")
        params = json.loads(row["params_json"])
        if param_overrides:
            params.update({str(k): str(v) for k, v in param_overrides.items()})
        if check is not None:
            refusal = check(str(row["agent"]), str(row["action_type"]), params)
            if refusal:
                raise ValueError(f"approval #{approval_id} refused: {refusal}")
        if param_overrides:
            self._conn.execute(
                "UPDATE approval_queue SET params_json = ? WHERE id = ?",
                (json.dumps(params), approval_id),
            )
        self._conn.execute(
            "UPDATE approval_queue SET status = ?, resolved_at = ? WHERE id = ?",
            (decision, _now(), approval_id),
        )
        self._conn.commit()
        return {"id": approval_id, "action_type": row["action_type"], "status": decision}

    def supersede_approval(self, approval_id: int) -> bool:
        """Close a PENDING card as ``superseded``: queue hygiene, never a
        decision (an execute path that selects approved cards moves nothing on
        its behalf). Returns False, writing nothing, if the card is not pending.
        Only the runner calls this, for a supersession a job declared."""
        cur = self._conn.execute(
            "UPDATE approval_queue SET status = 'superseded', resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (_now(), approval_id),
        )
        if not self._in_txn:
            self._conn.commit()
        return cur.rowcount == 1

    def approval_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """The approval row holding this subject key, or None.

        The runner uses this after a deduped enqueue to tell the designed
        silent case (pending card = the memory) from the lying one (resolved
        card swallowing a fresh ask — issue #161)."""
        row = self._conn.execute(
            "SELECT id, status, action_type FROM approval_queue WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "status": str(row["status"]),
            "action_type": str(row["action_type"]),
        }

    def pending_approvals(self, tenant: str | None = None) -> list[dict[str, Any]]:
        if tenant is None:
            rows = self._conn.execute(
                "SELECT * FROM approval_queue WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM approval_queue WHERE status = 'pending' AND tenant = ? ORDER BY id",
                (tenant,),
            ).fetchall()
        return [
            {
                "idempotency_key": r["idempotency_key"],
                "action_type": r["action_type"],
                "params": json.loads(r["params_json"]),
            }
            for r in rows
        ]

    # -- git ---------------------------------------------------------------

    def commit(self, *, agent: str, job: str, idempotency_key: str, summary: str) -> str | None:
        """Commit ledger writes with a structured message. None if no-op."""
        message = structured_message(agent, job, idempotency_key, summary)
        return commit_all(self._root, message)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_run(row: sqlite3.Row, *, is_new: bool) -> StoredRun:
        return StoredRun(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            tenant=row["tenant"],
            agent=row["agent"],
            job=row["job"],
            status=row["status"],
            shadow=bool(row["shadow"]),
            result_json=row["result_json"],
            summary=row["summary"],
            created_at=row["created_at"],
            is_new=is_new,
        )
