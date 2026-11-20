"""The bank's own balance against the books (board item t-books-page, part 1;
the owner, 2026-11-11).

A bank's CSV export says, after each line, what the account held. For the
ministry's account the expenses statement job keeps, for each statement
that carried a balance, one event (``bank.balances``): each day's closing
balance, and every line the statement held. The books are checked only
with the lines such a statement held, so a line from another account, or
from a statement sent with no balance, never reads as a gap (practice box,
2026-11-11: Ruth's earlier made-up account mixed in and read as eight).
Sending that statement again, with its balances, brings its lines in.

The earliest balance is where the books start: that balance, less the
lines up to it, is what the account held before its first line. Every
later balance checks the books, which are that opening plus every line
since: they agree, or the gap is named once, with the last day they agreed,
the first day they didn't, and the dollars (the bank's less the books', so
a line never kept reads negative when it was money out). After a gap the
bank's balance is the new start, so one missing statement is one gap,
never every day after it. One filer's ministry statements are read as one
account; a ministry with two accounts (checking and savings) is a later
build.

The person's own account never keeps a balance: it is their private life,
and only the lines they pick are ever the ministry's.

Pure functions over the event log: no I/O, no clock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .records import LINE_EVENT
from .statements import MINISTRY, Line
from .statements import day_words as _day

BALANCE_EVENT = "bank.balances"  # one ministry statement's closing balances and its lines


def key(person: str, days: dict[str, int], lines: list[str]) -> str:
    """The event's idempotency key: the same statement sent twice is one."""
    raw = json.dumps([person, MINISTRY, sorted(days.items()), sorted(lines)])
    return "bank-balances:" + hashlib.sha256(raw.encode()).hexdigest()


def payload(person: str, days: dict[str, int], lines: list[str], by: str = "") -> dict:
    return {
        "person": person,
        "account": MINISTRY,
        "days": dict(sorted(days.items())),
        "lines": sorted(lines),
        **({"by": by} if by else {}),
    }


def same(a: dict, b: dict) -> bool:
    """Whether two events hold the same statement's balances."""
    return (a.get("person"), a.get("days"), sorted(a.get("lines") or [])) == (
        b.get("person"),
        b.get("days"),
        sorted(b.get("lines") or []),
    )


@dataclass(frozen=True)
class Gap:
    """Where the bank and the books stopped agreeing."""

    after: str  # the last day they agreed
    on: str  # the first day they didn't
    cents: int  # the bank's balance less the books'


@dataclass(frozen=True)
class Standing:
    """One ministry account as of a day."""

    person: str  # who sent its statements
    opening: tuple[str, int]  # the first line's day, and what the account held before it
    bank_on: str  # the latest day the bank gave a balance
    bank: int  # that balance
    books: int  # what the books hold as of the day asked
    gaps: tuple[Gap, ...]


def standing(log: list[dict], through: str) -> list[Standing]:
    """Each ministry account whose statement gave a balance on or before
    ``through``, with the books against it: only the lines a statement with
    balances held, and nothing dated after ``through``. An account with no
    balance has nothing to say."""
    kept: dict[str, dict] = {}
    said: dict[str, dict[str, int]] = {}
    held: dict[str, set[str]] = {}
    for e in log:
        p = e.get("payload", {})
        if p.get("account") != MINISTRY:
            continue
        if e.get("event_type") == LINE_EVENT:
            kept.setdefault(str(p["sha256"]), p)
        elif e.get("event_type") == BALANCE_EVENT:
            person = str(p.get("person", ""))
            held.setdefault(person, set()).update(str(s) for s in p.get("lines") or [])
            for day, cents in (p.get("days") or {}).items():
                if str(day) <= through:
                    said.setdefault(person, {})[str(day)] = int(cents)  # the latest wins
    out = []
    for person, days in sorted(said.items()):
        lines = [
            Line.of(p)
            for sha, p in kept.items()
            if sha in held[person] and p.get("person") == person and str(p["day"]) <= through
        ]
        out.append(_one(person, lines, days))
    return out


def _one(person: str, lines: list[Line], days: dict[str, int]) -> Standing:
    by_day: dict[str, int] = {}
    for ln in lines:
        by_day[ln.day] = by_day.get(ln.day, 0) + ln.cents
    ordered = sorted(days)
    first = ordered[0]
    held = days[first]
    opening = held - sum(c for d, c in by_day.items() if d <= first)
    gaps, agreed = [], first
    for day in ordered[1:]:
        held += sum(c for d, c in by_day.items() if agreed < d <= day)
        if held != days[day]:
            gaps.append(Gap(after=agreed, on=day, cents=days[day] - held))
            held = days[day]
        agreed = day
    books = held + sum(c for d, c in by_day.items() if d > agreed)
    start = min([first, *by_day])
    return Standing(person, (start, opening), agreed, days[agreed], books, tuple(gaps))


def words(stand: Standing) -> str:
    """The bank against the books, for the person to hear."""
    said = f"The bank says {_dollars(stand.bank)} at the end of {_day(stand.bank_on)}"
    if not stand.gaps:
        if stand.books != stand.bank:
            return f"{said}; with the lines since, the books hold {_dollars(stand.books)}."
        return f"{said}, and the books agree."
    parts = [
        f"between {_day(g.after)} and {_day(g.on)} the bank has {_dollars(abs(g.cents))} "
        f"{'more' if g.cents > 0 else 'less'} than the books"
        for g in stand.gaps
    ]
    return (
        f"{said}, but {'; '.join(parts)}: a statement never sent, or a line kept twice. "
        "Ask for the statement that covers those days."
    )


def _dollars(cents: int) -> str:
    return f"-${-cents / 100:,.2f}" if cents < 0 else f"${cents / 100:,.2f}"


__all__ = ["BALANCE_EVENT", "Gap", "Standing", "key", "payload", "same", "standing", "words"]
