"""Tenant-local calendar arithmetic (#136).

The engine runs on UTC timestamps (the ledger's _now), but calendar
decisions — which month a receipt files under, what date a reimbursement
books, where an intake cutoff falls — are tenant-local facts. Slicing a
UTC timestamp for them shifts every evening event to the next day and
flips months hours early at month-end. Every helper takes the tenant's
IANA timezone name (ctx.tenant.identity.timezone) explicitly; ``at``
exists so tests pass a frozen instant instead of monkeypatching time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def local_now(tz: str, *, at: datetime | None = None) -> datetime:
    """The current (or supplied) instant viewed on the tenant's wall clock."""
    instant = at if at is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(ZoneInfo(tz))


def local_today(tz: str, *, at: datetime | None = None) -> str:
    """Today's ISO date on the tenant's wall clock."""
    return local_now(tz, at=at).date().isoformat()


def local_month(tz: str, *, at: datetime | None = None) -> str:
    """The current YYYY-MM on the tenant's wall clock."""
    return local_now(tz, at=at).strftime("%Y-%m")


def local_date(iso_ts: str, tz: str) -> str:
    """The ISO date of a stored timestamp viewed on the tenant's wall clock.

    Ledger timestamps are UTC ISO strings; a bare date passes through
    unchanged, and a timestamp with no offset is read as UTC (that is what
    the ledger writes).
    """
    text = str(iso_ts).strip()
    if len(text) <= 10:
        return text
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZoneInfo(tz)).date().isoformat()
