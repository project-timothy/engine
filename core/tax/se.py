"""Self-employment tax, Schedule SE (moved from the packet, board chunk
c36): the Social Security and Medicare a self-employed person pays both
halves of. 92.35% of net profit is their net earnings (line 4a); none is
owed below $400 of it (line 4c); Social Security is 12.4% up to the year's
wage base less the person's own W-2 wages (lines 8a and 9; a spouse's never
count) (line 10) and Medicare 2.9% on all of it (line 11), each line
rounded to the cent and added (line 12); half of it comes off their income
(line 13). The Foreign Earned Income Exclusion never lowers it."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import figures


def cents(x: Decimal) -> int:
    """An exact amount in cents, rounded half up to a whole cent as a form line is."""
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class SelfEmployment:
    net: int  # cents: income on a 1099 less the costs it carried
    earnings: int  # 92.35% of it
    tax: int
    half: int  # what comes off their income


def self_employment(net: int, year: str, *, wages: int = 0) -> SelfEmployment:
    """Self-employment tax on ``net`` cents for ``year``, with ``wages`` the
    person's own W-2 wages in cents (figures.NoYear, a KeyError, for a year
    with no figures: say so, never guess)."""
    y = figures.year(year)
    earnings = cents(Decimal(max(net, 0)) * y.rate("self_employment.earnings_share"))
    if earnings < y.cents("self_employment.floor"):
        return SelfEmployment(net, earnings, 0, 0)
    base = max(y.cents("self_employment.wage_base") - max(wages, 0), 0)  # line 9
    tax = cents(Decimal(min(earnings, base)) * y.rate("self_employment.social_security")) + cents(
        Decimal(earnings) * y.rate("self_employment.medicare")
    )
    return SelfEmployment(net, earnings, tax, cents(Decimal(tax) * y.rate("self_employment.half")))


def figures_from(year: str) -> str:
    """Where the year's self-employment figures came from."""
    return figures.year(year).cited(
        "self_employment.wage_base", "self_employment.earnings_share", "exclusion.most"
    )
