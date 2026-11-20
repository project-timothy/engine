"""What Tim's own voice costs, counted against the ministry's cap
(Standalone Tim, shot 1).

A job's model calls are counted on the job's own ledger connection, under
the run lock, for as long as the job runs (``core.llm.policy.complete_for``).
A conversation is not a job, and a model call takes seconds: holding the
ledger for that long would turn away a receipt someone sends in the
meantime. So this meter, a ``core.llm.policy.CallMeter``, touches the ledger
only around each small step:

- this month's spend is read through a read-only connection, the way every
  tool reads the books: no lock, never blocking a run;
- each ``llm_calls`` row is written under the ledger's write lock, taken
  just for the insert. A run in flight holds that lock, so the write waits
  a few seconds for it; if the run is still going, the row is written
  through SQLite's own lock instead. A call that happened is always on the
  record: an unrecorded call is how a ministry would slip past its cap.

The table is the one the nightly jobs' budget already reads, so a ministry
has one cap for everything a model does for it. Nothing here changes the
ledger's shape.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..engine.runner import LedgerLocked, ledger_write_lock
from ..ledger import Ledger
from ..ledger.ledger import DB_FILENAME
from ..llm import telemetry

WAIT_S = 3.0
"""How long a cost row waits for a run to let go of the ledger."""


class Meter:
    """A ministry's model spend, for a caller outside any job."""

    def __init__(self, ledger_root: str | Path, *, wait_s: float = WAIT_S) -> None:
        self.root = Path(ledger_root)
        self.wait_s = wait_s

    def _reading[T](self, read: Callable[[sqlite3.Connection], T], none: T) -> T:
        """``read`` over a read-only connection, or ``none`` on a ledger no
        model has ever been called for."""
        db = self.root / DB_FILENAME
        if not db.is_file():
            return none
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            return read(con)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return none
            raise
        finally:
            con.close()

    def month_to_date_usd(self, tenant: str, *, now: datetime | None = None) -> Decimal:
        return self._reading(
            lambda con: telemetry.month_to_date_usd(con, tenant, now=now), Decimal("0")
        )

    def month_by_job(self, tenant: str, *, now: datetime | None = None) -> dict[str, dict]:
        """This month's calls, refusals, and spend, by job type."""
        return self._reading(lambda con: telemetry.month_by_job(con, tenant, now=now), {})

    def record_call(self, **row: Any) -> int:
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                with ledger_write_lock(self.root), Ledger.open(self.root) as ledger:
                    return telemetry.record_call(ledger.conn, **row)
            except LedgerLocked:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        con = sqlite3.connect(str(self.root / DB_FILENAME), timeout=10)
        try:
            return telemetry.record_call(con, **row)
        finally:
            con.close()


def soft_line(budget: Any) -> Decimal | None:
    """The spend past which Tim stops stepping up (``[llm.budget]``'s
    ``soft_percent`` of ``monthly_usd``, 80 unless the file says), to the
    cent; None where the ministry has no cap."""
    cap = budget.monthly_usd
    if cap is None:
        return None
    return (Decimal(cap) * budget.soft_percent / 100).quantize(Decimal("0.01"))


def spend(settings: Any, meter: Meter, tenant: str, *, now: datetime | None = None) -> dict:
    """What a ministry's models cost this month (``engine spend``, card V3):
    the total, the cap and the soft line, and each job type's calls,
    refusals, and dollars. Money is a decimal string."""
    instant = now or datetime.now(UTC)
    by_job = meter.month_by_job(tenant, now=instant)
    total = sum((held["usd"] for held in by_job.values()), Decimal("0"))
    cap = settings.budget.monthly_usd
    soft = soft_line(settings.budget)
    return {
        "tenant": tenant,
        "month": instant.astimezone(UTC).strftime("%Y-%m"),
        "spent_usd": str(total),
        "cap_usd": None if cap is None else str(cap),
        "soft_usd": None if soft is None else str(soft),
        "soft_percent": settings.budget.soft_percent,
        "past_soft_line": soft is not None and total >= soft,
        "capped": cap is not None and total >= cap,
        "by_job": {job: {**held, "usd": str(held["usd"])} for job, held in sorted(by_job.items())},
    }


def _cents(text: str) -> str:
    return f"${Decimal(text).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def spend_line(report: dict) -> str:
    """The same report as one plain line, for the owner's board."""
    cap = f" of {_cents(report['cap_usd'])}" if report["cap_usd"] is not None else ", no cap"
    soft = f" (soft line {_cents(report['soft_usd'])})" if report["soft_usd"] is not None else ""
    parts = [
        f"{job} {held['calls']} call{'' if held['calls'] == 1 else 's'} {_cents(held['usd'])}"
        + (f", {held['refused']} turned away" if held["refused"] else "")
        for job, held in report["by_job"].items()
    ]
    state = ""
    if report["capped"]:
        state = " CAPPED."
    elif report["past_soft_line"]:
        state = " Past the soft line."
    jobs = f" {'; '.join(parts)}." if parts else " No model calls yet."
    return (
        f"{report['tenant']} {report['month']}: {_cents(report['spent_usd'])}{cap} spent{soft}."
        f"{state}{jobs}"
    )
