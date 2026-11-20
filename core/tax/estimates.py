"""Estimated tax payments under the safe-harbor rules (board chunk c39, PR 2;
moved here from ``form1040.each_estimate``), by the Form 1040-ES worksheet:

- line 12a: 90% of the tax expected for the year, rounded to the cent;
- line 12b: last year's total tax, or 110% of it when last year's adjusted
  gross income passed $150,000 (left out when not known);
- line 12c: the smaller of the two;
- line 13: the withholding expected;
- line 14a: line 12c less line 13, the year's required payment;
- line 14b: the expected tax less withholding; under $1,000 and nothing is
  required.

Each payment is a quarter of line 14a; ``four`` spreads it so the four add
up exactly. Payments already made show against the year (``paid``, ``left``).
Pure: cents in, cents out."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from . import figures
from .se import cents

THIS_YEAR = "this_year"  # 90% of the tax expected this year
LAST_YEAR = "last_year"  # last year's tax (110% above the line)


@dataclass(frozen=True)
class Plan:
    year: str
    required: int  # line 14a
    each: int  # a quarter of it
    by: str  # which safe harbor set it: THIS_YEAR or LAST_YEAR
    paid: int = 0  # estimates already paid toward the year
    left: int = 0  # required less paid, never below zero


def plan(
    year: str,
    expected: int,
    *,
    withheld: int = 0,
    last_year_tax: int | None = None,
    last_year_agi: int | None = None,
    paid: Iterable[tuple[str, int]] = (),
) -> Plan:
    """The year's estimated payments on ``expected`` cents of total tax."""
    y = figures.year(year)
    this_year = cents(Decimal(expected) * y.rate("estimates.this_year_share"))  # line 12a
    by, chosen = THIS_YEAR, this_year
    if last_year_tax is not None:  # line 12b
        high = last_year_agi is not None and last_year_agi > y.cents("estimates.high_income")
        share = y.rate(
            "estimates.last_year_share_high_income" if high else "estimates.last_year_share"
        )
        last = cents(Decimal(max(last_year_tax, 0)) * share)
        if last < this_year:
            by, chosen = LAST_YEAR, last
    required = chosen - withheld  # line 14a
    if required <= 0 or expected - withheld < y.cents("estimates.owed_at"):  # line 14b
        required = 0
    done = sum(c for _, c in paid)
    return Plan(year, required, cents(Decimal(required) / 4), by, done, max(required - done, 0))


def each(year: str, total: int, *, withheld: int = 0) -> int:
    """Each of the four payments on ``total`` cents of tax, with no
    last-year figure: line 14a over four."""
    return plan(year, total, withheld=withheld).each


def four(required: int) -> list[int]:
    """The four payments, a quarter each, the odd cents on the first."""
    quarter, extra = divmod(max(required, 0), 4)
    return [quarter + extra, quarter, quarter, quarter]
