"""The span of the books a person asks about (board item t-books; the owner,
2026-10-29: month end, quarter and year, for every role, at any time, year
to date too).

A person says it their way ("last quarter", "Q3", "year to date", "2025",
"September"); their AI passes the words or a short form, and this reads it
to a first and last day. Pure: no clock of its own, ``today`` is passed.
Nothing here is a tenant's: a ministry's year is the calendar year, as the
budget's is."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = (
    "january february march april may june july august september october november december"
).split()


@dataclass(frozen=True)
class Period:
    """A first and last day, and how to say it."""

    start: date
    end: date
    words: str
    year_to_date: bool = False

    def months(self, today: date) -> list[str]:
        """Each month (YYYY-MM) in the span, through ``today``'s month."""
        out, m = [], self.start.replace(day=1)
        while m <= self.end and m <= today:
            out.append(m.strftime("%Y-%m"))
            m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        return out


class NotAPeriod(ValueError):
    pass


def _month_end(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def month(year: int, m: int) -> Period:
    first = date(year, m, 1)
    return Period(first, _month_end(first), f"{first:%B %Y}")


def quarter(year: int, q: int) -> Period:
    first = date(year, 3 * q - 2, 1)
    return Period(first, _month_end(date(year, 3 * q, 1)), f"Q{q} {year}")


def year(y: int) -> Period:
    return Period(date(y, 1, 1), date(y, 12, 31), str(y))


def year_to_date(y: int, today: date) -> Period:
    if y < today.year:
        return year(y)
    return Period(date(y, 1, 1), today, f"{y} through {today:%B} {today.day}", True)


def read(said: str, today: date) -> Period:
    """``said`` as a span: YYYY-MM, YYYY-Qn, YYYY, YYYY-YTD, a month's name
    (this year's, or last year's when it hasn't come yet), or this/last
    month, quarter or year, or year to date. Empty is year to date."""
    s = re.sub(r"\s+", " ", said.strip().lower().replace("_", " "))
    q_now = (today.month - 1) // 3 + 1
    if s in ("", "ytd", "year to date", "this year so far", "so far this year"):
        return year_to_date(today.year, today)
    if s in ("this month", "month to date"):
        return month(today.year, today.month)
    if s == "last month":
        last = today.replace(day=1) - timedelta(days=1)
        return month(last.year, last.month)
    if s in ("this quarter", "quarter to date"):
        return quarter(today.year, q_now)
    if s == "last quarter":
        return quarter(today.year, q_now - 1) if q_now > 1 else quarter(today.year - 1, 4)
    if s == "this year":
        return year_to_date(today.year, today)
    if s == "last year":
        return year(today.year - 1)
    if m := re.fullmatch(r"(\d{4})-(\d{2})", s):
        y, mo = int(m[1]), int(m[2])
        if 1 <= mo <= 12:
            return month(y, mo)
    if m := re.fullmatch(r"(\d{4}) ?-? ?q([1-4])|q([1-4]) ?(\d{4})?", s):
        y = int(m[1] or m[4] or today.year)
        return quarter(y, int(m[2] or m[3]))
    if m := re.fullmatch(r"(\d{4})", s):
        return year(int(m[1]))
    if m := re.fullmatch(r"(\d{4}) ?-? ?ytd|ytd ?(\d{4})", s):
        return year_to_date(int(m[1] or m[2]), today)
    if m := re.fullmatch(r"([a-z]+)(?: (\d{4}))?", s):
        name = m[1]
        hit = [i for i, n in enumerate(_MONTHS, 1) if len(name) >= 3 and n.startswith(name)]
        if len(hit) == 1:
            y = int(m[2]) if m[2] else today.year - (hit[0] > today.month)
            return month(y, hit[0])
    raise NotAPeriod(said)


SAY_PERIOD = (
    "a month (2026-09 or September), a quarter (2026-Q3, last quarter), a year (2025, "
    "last year), or year to date (YTD)"
)
