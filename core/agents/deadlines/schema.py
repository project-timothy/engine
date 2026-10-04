"""Deadlines: the obligations file and the date arithmetic.

Boundary classification (docs/boundary-rules.md): **code**, row 1. A due date,
its recurrence and its lead windows are binary-correct and computable from
structured input; no model is on this path. The construct is the
**Reminder**: a dated obligation the engine cannot execute itself (renew a
passport, file a return, renew a registration), surfaced ahead of time with
its evidence. The owner's act is the act itself; ``deadlines done`` records it.

The obligations file is tenant data, ``obligations.toml`` beside
``tenant.toml``::

    [[obligation]]
    id = "passport-jane"          # stable slug: lowercase, digits, - and _
    title = "Passport renewal"
    who = "Jane Doe"              # the person or unit it belongs to
    kind = "passport"             # a free label: visa, tax, insurance, report...
    due = 2027-03-01              # a TOML date; for a recurring one, the anchor
    every = "1y"                  # optional: <n>d, <n>w, <n>m or <n>y
    lead_days = [90, 30, 7]       # optional; defaults to [deadlines].lead_days
    notes = "..."                 # optional, shown on the reminder
    evidence = "..."              # optional: a path or link to the paperwork
"""

from __future__ import annotations

import calendar
import re
import tomllib
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

DEFAULT_LEAD_DAYS = (90, 30, 7)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EVERY_RE = re.compile(r"^([1-9][0-9]{0,2})([dwmy])$")


class ObligationsError(ValueError):
    """The obligations file cannot be read; the message names the problem."""


class Obligation(BaseModel):
    id: str
    title: str
    who: str = ""
    kind: str = ""
    due: date
    every: str | None = None
    lead_days: list[int] | None = None
    notes: str = ""
    evidence: str = ""

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"id {v!r} must be a lowercase slug (a-z, 0-9, - and _)")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title is empty")
        return v.strip()

    @field_validator("every")
    @classmethod
    def _every(cls, v: str | None) -> str | None:
        if v is not None and not _EVERY_RE.match(v):
            raise ValueError(f"every {v!r} must look like 10d, 2w, 3m or 1y")
        return v

    @field_validator("lead_days")
    @classmethod
    def _leads(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if not v or any(d < 0 or d > 3650 for d in v):
            raise ValueError("lead_days must be whole days between 0 and 3650")
        return sorted(set(v), reverse=True)

    def leads(self, default: tuple[int, ...] | list[int]) -> list[int]:
        return list(self.lead_days) if self.lead_days else sorted(set(default), reverse=True)


class ObligationsFile(BaseModel):
    obligation: list[Obligation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> ObligationsFile:
        seen: set[str] = set()
        for ob in self.obligation:
            if ob.id in seen:
                raise ValueError(f"duplicate obligation id {ob.id!r}")
            seen.add(ob.id)
        return self


def load_obligations(path: Path) -> list[Obligation]:
    """Every obligation in the file, validated. Raises :class:`ObligationsError`
    naming the problem: a bad file is never read as an empty one."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ObligationsError(f"{Path(path).name} is not valid TOML: {exc}") from exc
    try:
        return ObligationsFile.model_validate(data).obligation
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ObligationsError(f"{Path(path).name}: {problems}") from None


def _add_months(anchor: date, months: int) -> date:
    """``anchor`` plus whole months, clamped to the month's last day. Always
    computed from the anchor, so Jan 31 gives Feb 28 then Mar 31, never a
    creep to the 28th."""
    total = anchor.month - 1 + months
    year, month = anchor.year + total // 12, total % 12 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def nth_occurrence(anchor: date, every: str | None, n: int) -> date:
    if not every or n == 0:
        return anchor
    match = _EVERY_RE.match(every)
    if match is None:  # validated at load; a direct caller gets the same rule
        raise ObligationsError(f"every {every!r} must look like 10d, 2w, 3m or 1y")
    count, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        return anchor + timedelta(days=count * n)
    if unit == "w":
        return anchor + timedelta(weeks=count * n)
    if unit == "m":
        return _add_months(anchor, count * n)
    return _add_months(anchor, 12 * count * n)


def occurrence_dates(anchor: date, every: str | None, *, until: date) -> list[date]:
    """Every occurrence from the anchor up to and including ``until``. A
    one-off obligation has exactly one, whatever ``until`` is."""
    if not every:
        return [anchor]
    out: list[date] = []
    n = 0
    while (d := nth_occurrence(anchor, every, n)) <= until:
        out.append(d)
        n += 1
    return out
