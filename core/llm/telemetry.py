"""The ``llm_calls`` store (phase 7 row 7.9): one row per gateway call.

The gateway returns a :class:`~core.llm.gateway.CallRecord` and writes
nothing; the policy layer persists it here, through the ledger's sanctioned
``conn`` path, the way every domain table has its own store module. Three
readers: the policy's monthly budget check (this month's spend for the
tenant), the runner's run totals (the rows under one run key), and the
runner's anomaly pass (the budget refusals under one run key).

Money is a decimal STRING in the row and a ``Decimal`` in Python; a float
never touches it. Rows commit at once (a call happened whether or not the
job that made it lands), the same rule as job records.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

STATUS_OK = "ok"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_TRANSPORT_FAILED = "transport_failed"
STATUS_BUDGET_REFUSED = "budget_refused"
STATUS_DOCUMENTS_REFUSED = "documents_refused"
STATUSES = (
    STATUS_OK,
    STATUS_VALIDATION_FAILED,
    STATUS_TRANSPORT_FAILED,
    STATUS_BUDGET_REFUSED,
    STATUS_DOCUMENTS_REFUSED,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_call(
    conn: sqlite3.Connection,
    *,
    tenant: str,
    job_type: str,
    run_key: str,
    tier: str,
    adapter: str,
    model: str,
    provider_model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    usd: Decimal = Decimal("0"),
    retries: int = 0,
    latency_ms: int = 0,
    status: str = STATUS_OK,
    detail: str = "",
    created_at: str | None = None,
) -> int:
    """Insert one row and commit it. Returns the row id."""
    if status not in STATUSES:
        raise ValueError(f"llm_calls status {status!r} is not one of {', '.join(STATUSES)}")
    cur = conn.execute(
        """
        INSERT INTO llm_calls
            (tenant, job_type, run_key, tier, adapter, model, provider_model,
             tokens_in, tokens_out, usd, retries, latency_ms, status, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant,
            job_type,
            run_key,
            tier,
            adapter,
            model,
            provider_model,
            int(tokens_in),
            int(tokens_out),
            str(usd),
            int(retries),
            int(latency_ms),
            status,
            detail,
            created_at or _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def month_to_date_usd(
    conn: sqlite3.Connection, tenant: str, *, now: datetime | None = None
) -> Decimal:
    """The tenant's recorded spend since the first instant of the current
    UTC month. Ledger timestamps are UTC ISO strings, so the month boundary
    is a string prefix compare on ``created_at``; ``now`` is injectable for
    the tests (a frozen instant, never a monkeypatched clock)."""
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    start = instant.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute(
        "SELECT usd FROM llm_calls WHERE tenant = ? AND created_at >= ?",
        (tenant, start.isoformat()),
    ).fetchall()
    return sum((Decimal(str(r[0])) for r in rows), Decimal("0"))


def run_totals(conn: sqlite3.Connection, run_key: str) -> dict[str, Any]:
    """The rows under one run key, summed: ``calls`` (every gateway call,
    ok or failed; refusals excluded), ``tokens_in``, ``tokens_out``, ``usd``
    (a Decimal)."""
    rows = conn.execute(
        "SELECT status, tokens_in, tokens_out, usd FROM llm_calls WHERE run_key = ?",
        (run_key,),
    ).fetchall()
    calls = sum(1 for r in rows if r[0] != STATUS_BUDGET_REFUSED)
    return {
        "calls": calls,
        "tokens_in": sum(int(r[1]) for r in rows),
        "tokens_out": sum(int(r[2]) for r in rows),
        "usd": sum((Decimal(str(r[3])) for r in rows), Decimal("0")),
    }


def budget_refusals(conn: sqlite3.Connection, run_key: str) -> list[dict[str, Any]]:
    """The budget refusals recorded under one run key, oldest first."""
    rows = conn.execute(
        "SELECT job_type, tier, model, detail, created_at FROM llm_calls "
        "WHERE run_key = ? AND status = ? ORDER BY id",
        (run_key, STATUS_BUDGET_REFUSED),
    ).fetchall()
    return [
        {"job_type": r[0], "tier": r[1], "model": r[2], "detail": r[3], "created_at": r[4]}
        for r in rows
    ]
