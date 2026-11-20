"""What this ministry paid a person as pay in a year (gap A of the 11/15
plan; board chunk c34, Session 5).

One answer for one fact. Pay is a line out of the ministry's own account
with the role "their pay" and whom it paid (c34 PR 2). The church's 1099
total and the person's tax packet both read it here, so the two January
numbers are the same number.

A gift to the ministry given toward a missionary it supports carries
``for_fund`` (c34 PR 3). Their fund is a view over the books: those gifts
less their pay and what the ministry paid them back, never an account of
its own (the owner's call of 2026-11-14)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal

from .statements import CAME_IN, PAID_BACK, PAY_OUT, money


def _cents(r: dict) -> int:
    return abs(int(Decimal(str(r["dollars"])) * 100))


def _pay(rows: Iterable[dict], year: str) -> Iterable[dict]:
    seen: set[tuple] = set()
    for r in rows:
        if r.get("role") != PAY_OUT or not r.get("paid_to") or str(r["date"])[:4] != year:
            continue
        key = (r.get("source") or (r["date"], r["dollars"], r.get("words")), r["paid_to"])
        if key not in seen:  # the same line read under two people's rows counts once
            seen.add(key)
            yield r


def paid_as_pay(rows: Iterable[dict], person: str, year: str) -> list[dict]:
    """Each line this ministry paid ``person`` as pay in ``year``, by date:
    ``{"date", "dollars"}`` (month_report bank rows of the year, anyone's)."""
    return [
        {"date": r["date"], "dollars": money(_cents(r))}
        for r in sorted(_pay(rows, year), key=lambda r: r["date"])
        if r["paid_to"] == person
    ]


def by_person(rows: Iterable[dict], year: str) -> dict[str, int]:
    """What this ministry paid each person as pay in ``year``, in cents."""
    out: dict[str, int] = {}
    for r in _pay(rows, year):
        out[r["paid_to"]] = out.get(r["paid_to"], 0) + _cents(r)
    return dict(sorted(out.items()))


def funds(
    rows: Iterable[dict], year: str, name_of: Callable[[str], str] = str
) -> dict[str, dict[str, str]]:
    """Each fund in ``year``, by whom it is for: the gifts given toward it,
    their pay, what the ministry paid them back, and what is left (below
    zero when it paid out more than was given). Only people with a gift
    given toward them have a fund. A line that paid someone back names them
    as the books carry them (``name_of`` their id), so either matches."""
    rows = [r for r in rows if str(r["date"])[:4] == year]
    gifts: dict[str, int] = {}
    for r in rows:
        if r.get("role") == CAME_IN and r.get("for_fund"):
            gifts[r["for_fund"]] = gifts.get(r["for_fund"], 0) + _cents(r)
    pay = by_person(rows, year)
    known = {n.casefold(): who for who in gifts for n in (who, name_of(who))}
    back: dict[str, int] = {}
    for r in rows:
        who = known.get(str(r.get("paid_to") or "").casefold())
        if r.get("role") == PAID_BACK and who:
            back[who] = back.get(who, 0) + _cents(r)
    return {
        who: {
            "gifts": money(given),
            "pay": money(pay.get(who, 0)),
            "paid_back": money(back.get(who, 0)),
            "left": money(given - pay.get(who, 0) - back.get(who, 0)),
        }
        for who, given in sorted(gifts.items())
    }
