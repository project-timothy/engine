"""The IRS standard mileage rate and what a drive is worth at it (board
chunk c34, PR 6).

The rate comes from the year's table (``mileage.business_to_june_30`` and
``mileage.business_from_july_1`` in ``tables/<year>.toml``), on the drive's
own day. A ministry pays a drive back at its own rate, never above the IRS
rate: anything above it counts as pay. When its rate is lower, what the
drive was worth at the IRS rate less what was paid back is a car cost the
person carries on Schedule C, line 9."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import figures


def irs_rate(day: date) -> Decimal:
    """The IRS business rate per mile on ``day``. Raises ``figures.NoYear``
    for a year Tim holds no table for."""
    half = "from_july_1" if day.month >= 7 else "to_june_30"
    return figures.year(str(day.year)).rate(f"mileage.business_{half}")


def dollars(miles: Decimal, rate: Decimal) -> Decimal:
    """A drive in dollars at a rate per mile, to the cent."""
    return (miles * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def below_irs(drives: Iterable[tuple[Decimal, Decimal, Decimal]]) -> int:
    """Cents: what the drives were worth at the IRS rate less what was paid
    back, each drive ``(miles, irs_rate, paid_rate)``."""
    return sum(
        int((dollars(miles, irs) - dollars(miles, paid)) * 100)
        for miles, irs, paid in drives
        if paid < irs
    )
