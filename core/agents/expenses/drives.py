"""A ministry drive priced at two rates (board chunk c34, PR 6): what it is
worth at the IRS rate on its own day, and what the ministry pays it back
at (``core.institution.mileage_rate``). The month's listing, the report's
lines, and the tax packet all price a drive here. A drive that can't be
priced (a ministry rate above the IRS rate, a year with no table) waits,
and says why in plain words."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal

from ...engine.config import InstitutionSettings
from ...institution import InstitutionError, irs_mileage_rate, mileage_rate
from . import records


def priced(miles: Decimal, settings: InstitutionSettings, day: str) -> dict:
    """The drive's money: ``rate``, ``rate_from``, ``dollars`` (paid back),
    and ``irs_rate`` and ``at_irs_rate`` beside them when the ministry pays
    less; or ``refused``, the plain words for why it can't be priced."""
    on = date.fromisoformat(day)
    try:
        rate, rate_from = mileage_rate(settings, on)
        irs = irs_mileage_rate(on)
    except InstitutionError as why:
        return {"refused": str(why)}
    out = {"rate": rate, "rate_from": rate_from, "dollars": records.drive_dollars(miles, rate)}
    if rate < irs:
        out |= {"irs_rate": irs, "at_irs_rate": records.drive_dollars(miles, irs)}
    return out


def line(e: dict, word: dict, settings: InstitutionSettings, day: str, source: str) -> dict:
    """One drive of the month's listing: its miles, what the person said it
    was for, and its dollars when it was the ministry's."""
    miles = Decimal(str(e.get("miles", "0")))
    ministry = bool(word.get("ministry")) if word else None
    money = priced(miles, settings, day) if ministry else {}
    waiting = None if word else "what it was for"
    out = {
        "date": day,
        "miles": str(miles),
        "for": word.get("said"),
        "ministry": ministry,
        "dollars": f"{money['dollars']:.2f}" if "dollars" in money else None,
        "rate": str(money["rate"]) if "rate" in money else None,
        "rate_from": money.get("rate_from"),
        "waiting_on": money.get("refused") or waiting,
        "source": source,
    }
    if "irs_rate" in money:
        out |= {"irs_rate": str(money["irs_rate"]), "at_irs_rate": f"{money['at_irs_rate']:.2f}"}
    return out


def of_year(
    drives: Iterable[dict],
    said: dict[str, dict],
    settings: InstitutionSettings,
    year: str,
    whose: Callable[[str], bool],
) -> list[tuple[Decimal, Decimal, Decimal]]:
    """The person's ministry drives in ``year`` that the ministry pays back
    below the IRS rate, each ``(miles, irs_rate, paid_rate)`` for
    ``core.tax.mileage.below_irs``. ``said`` is each drive's purpose, by
    drive id."""
    out = []
    for e in drives:
        day = str(e.get("start", ""))[:10]
        if day[:4] != year or not whose(str(e.get("person", ""))):
            continue
        if not said.get(records.drive_id(e), {}).get("ministry"):
            continue
        miles = Decimal(str(e.get("miles", "0")))
        money = priced(miles, settings, day)
        if "irs_rate" in money:
            out.append((miles, money["irs_rate"], money["rate"]))
    return out
