"""What only the person can tell Tim for their taxes (board chunk c38; the
11/15 plan, section 2.4 gaps E, F, and H).

Two kinds of what a person said, each kept as an event (no migration):

- ``FACT_EVENT``: one fact for a tax year. Their filing status (single, or
  married filing jointly: the owner, 2026-11-15, decision 1); a spouse's
  wages and federal tax withheld, always said as one pair; last year's total
  tax; an estimated payment, with the day it was paid. Newest wins, field by
  field. A share (who else may read their packet) belongs to no year.
- ``TRAVEL_EVENT``: one trip, the day they left the United States, the day
  they came back (empty while they're still away), and the country. Newest
  wins per day left; a trip said forgotten is dropped.

Only the person says or reads these; they share their packet with whom they
choose (the owner, 2026-11-15, decision 2). Pure readers, plus the two jobs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ...engine.contracts import JobContext, JobOutput
from .payers import keep_said, said_key

FACT_EVENT = "tax.fact"
TRAVEL_EVENT = "tax.travel"

SINGLE, JOINT = "single", "married_jointly"
_STATUS_SAID = {
    "single": SINGLE,
    "unmarried": SINGLE,
    "not married": SINGLE,
    "married filing jointly": JOINT,
    "married jointly": JOINT,
    "married filing joint": JOINT,
    "filing jointly": JOINT,
    "jointly": JOINT,
    "joint": JOINT,
    "married": JOINT,
    "mfj": JOINT,
}
OTHER_STATUS = (
    "married filing separately",
    "separately",
    "head of household",
    "qualifying surviving spouse",
    "widow",
    "widower",
)
"""Statuses Tim leaves to the preparer (decision 1)."""

STATUS_WORDS = {SINGLE: "single", JOINT: "married filing jointly"}


def status_of(said: str) -> str:
    """The filing status plain words name: SINGLE, JOINT, "other" for one
    Tim leaves to the preparer, or "" when they name none."""
    words = " ".join(str(said or "").casefold().replace("-", " ").split())
    if any(o in words for o in OTHER_STATUS):
        return "other"
    return _STATUS_SAID.get(words, "")


@dataclass(frozen=True)
class Facts:
    status: str = ""
    spouse_wages: int | None = None  # cents; set only with spouse_withheld
    spouse_withheld: int | None = None
    last_year_tax: int | None = None
    estimates_paid: tuple[tuple[str, int], ...] = ()  # (day, cents), by day
    support_elsewhere: str = ""  # "none" or "some": asked once (persona night Y07); "" not yet


def facts(said: Iterable[dict], person: str, year: str) -> Facts:
    """A person's facts for ``year``; the newest word on each stands. An
    estimate said again for the same day replaces it; $0 drops it."""
    got: dict[str, str] = {}
    paid: dict[str, int] = {}
    for f in said:
        if f.get("person") != person or f.get("year") != year:
            continue
        for k in (
            "status",
            "spouse_wages",
            "spouse_withheld",
            "last_year_tax",
            "support_elsewhere",
        ):
            if f.get(k):
                got[k] = str(f[k])
        if f.get("estimate_day"):
            paid[str(f["estimate_day"])] = int(f.get("estimate_cents") or 0)
    return Facts(
        status=got.get("status", ""),
        spouse_wages=int(got["spouse_wages"]) if "spouse_wages" in got else None,
        spouse_withheld=int(got["spouse_withheld"]) if "spouse_withheld" in got else None,
        last_year_tax=int(got["last_year_tax"]) if "last_year_tax" in got else None,
        estimates_paid=tuple(sorted((d, c) for d, c in paid.items() if c > 0)),
        support_elsewhere=got.get("support_elsewhere", ""),
    )


NO_ONE = "no one"


def shared_with(said: Iterable[dict], person: str) -> tuple[str, ...]:
    """Who else may read ``person``'s packet (their units), as they last said."""
    last = ""
    for f in said:
        if f.get("person") == person and f.get("shared_with"):
            last = str(f["shared_with"])
    return tuple(u for u in last.split(";") if u and u != NO_ONE)


@dataclass(frozen=True)
class Trip:
    left: str  # YYYY-MM-DD, the day they left the United States
    back: str  # the day they came back; "" while still away
    country: str


def trips(said: Iterable[dict], person: str) -> list[Trip]:
    """A person's trips, by the day they left; the newest word on a day stands."""
    by_day: dict[str, Trip | None] = {}
    for t in said:
        if t.get("person") != person or not t.get("left"):
            continue
        left = str(t["left"])
        by_day[left] = (
            None
            if t.get("forget")
            else Trip(left, str(t.get("back") or ""), str(t.get("country") or ""))
        )
    return [trip for _day, trip in sorted(by_day.items()) if trip is not None]


# ---- the jobs that keep what was said ---------------------------------------------------

_FACT = (
    "person",
    "year",
    "status",
    "spouse_wages",
    "spouse_withheld",
    "last_year_tax",
    "estimate_day",
    "estimate_cents",
    "shared_with",
    "support_elsewhere",
    "by",
)
_TRAVEL = ("person", "left", "back", "country", "forget", "by")


def fact_key(ctx: JobContext) -> str:
    return said_key(ctx, "taxfact", FACT_EVENT, _FACT)


def fact_run(ctx: JobContext) -> JobOutput:
    """Keep a tax fact as said (the door read the words and the dollars)."""
    return keep_said(ctx, FACT_EVENT, _FACT, "tax fact")


def travel_key(ctx: JobContext) -> str:
    return said_key(ctx, "taxtravel", TRAVEL_EVENT, _TRAVEL)


def travel_run(ctx: JobContext) -> JobOutput:
    """Keep a trip as said (the door read the days)."""
    return keep_said(ctx, TRAVEL_EVENT, _TRAVEL, "trip")
