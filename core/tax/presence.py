"""Days abroad for the physical presence test (board chunk c38, PR 3; the
plan of 2026-11-15, gap E): the Foreign Earned Income Exclusion needs 330
full days outside the United States in any 12 months in a row (Internal
Revenue Code section 911(d)(1)(B); IRS Publication 54, chapter 4).

A full day is a whole day abroad: the day they left the United States and
the day they came back are not full days. A 12-month period starts on any
day and ends the day before the same day a year later, 365 days, or 366
when it spans February 29. A stay still open (no day back) runs to the end
of whatever period is asked about. The best period is the one with the most
full days, among those that reach into the tax year; of two as good, the
one with more of its days in the tax year, then the earlier.

Pure: the travel dates come in as said (``tax.travel``, c38 PR 1), and the
bank's foreign spending stays the packet's cross-check, never this count."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

NEEDS = 330  # full days abroad in 12 months in a row
_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Stay:
    """A stretch abroad: the day they left the United States, and the day
    they came back ("" while still away)."""

    left: str  # YYYY-MM-DD
    back: str = ""


@dataclass(frozen=True)
class Presence:
    days: int  # full days abroad in the best 12 months
    start: date | None  # the period's first day (None with no stays)
    end: date | None  # its last day
    ahead: int = 0  # days of the period after today: theirs only if they stay

    @property
    def qualifies(self) -> bool:
        return self.days >= NEEDS


def year_later(day: date) -> date:
    """The last day of the 12 months that start on ``day``."""
    try:
        return day.replace(year=day.year + 1) - _DAY
    except ValueError:  # February 29: the same day a year later is March 1
        return date(day.year + 1, 3, 1) - _DAY


def full_days(stays: Iterable[Stay], until: date | None = None) -> set[date]:
    """Every whole day abroad: after the day they left, before the day they
    came back; a stay still open runs through ``until`` (none counted
    without it)."""
    out: set[date] = set()
    for s in stays:
        first = date.fromisoformat(s.left) + _DAY
        last = date.fromisoformat(s.back) - _DAY if s.back else until
        day = first
        while last is not None and day <= last:
            out.add(day)
            day += _DAY
    return out


def best(stays: Iterable[Stay], year: str, today: date | None = None) -> Presence:
    """The 12 months in a row with the most full days abroad, among those
    that reach into ``year``; ``ahead`` names the days of it after
    ``today``, which count only if they stay."""
    stays = list(stays)
    first, last = date(int(year), 1, 1), date(int(year), 12, 31)
    lo = first.replace(year=first.year - 1) + _DAY  # its period ends on January 1
    horizon = year_later(last)
    abroad = full_days(stays, until=horizon)
    if not abroad:
        return Presence(0, None, None)
    # running count of full days before each day, from lo to the horizon
    before: dict[date, int] = {}
    n, day = 0, lo
    while day <= horizon + _DAY:
        before[day] = n
        n += day in abroad
        day += _DAY
    pick: tuple[int, int, int] | None = None
    found: tuple[date, date] | None = None
    start = lo
    while start <= last:
        end = year_later(start)
        days = before[end + _DAY] - before[start]
        inside = (min(end, last) - max(start, first)).days + 1
        key = (days, inside, -start.toordinal())
        if pick is None or key > pick:
            pick, found = key, (start, end)
        start += _DAY
    if pick is None or found is None:  # a year has days, so never; kept for the type
        return Presence(0, None, None)
    ahead = (found[1] - today).days if today is not None and found[1] > today else 0
    return Presence(pick[0], found[0], found[1], ahead)


def in_year(stays: Iterable[Stay], p: Presence, year: str) -> tuple[int, int]:
    """For a qualifying period ``p``: the days of it that fall in ``year``
    (Form 2555 line 36, the qualifying days), and the full days abroad among
    them (what the year's income is split by, ``exclusion.earned_abroad``)."""
    if p.start is None or p.end is None:
        return 0, 0
    first = max(p.start, date(int(year), 1, 1))
    last = min(p.end, date(int(year), 12, 31))
    if last < first:
        return 0, 0
    abroad = full_days(stays, until=p.end)
    return (last - first).days + 1, sum(1 for d in abroad if first <= d <= last)
