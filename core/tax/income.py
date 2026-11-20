"""Income tax from the year's brackets (board chunk c36): the printed base
amount of the bracket the taxable income falls in, plus its rate on the
excess over that bracket's floor, rounded to the cent. A filed return reads
the IRS Tax Table under $100,000 and lands within a few dollars of this."""

from __future__ import annotations

from decimal import Decimal

from . import figures
from .se import cents


def _row(taxable: int, year: str, status: str) -> figures.Bracket:
    rows = figures.year(year).brackets(status)
    for row in rows:
        if row.up_to is None or taxable <= row.up_to:
            return row
    return rows[-1]


def tax_on(taxable: int, year: str, status: str) -> int:
    """Income tax in cents on ``taxable`` cents of taxable income."""
    if taxable <= 0:
        return 0
    row = _row(taxable, year, status)
    return row.base + cents(Decimal(taxable - row.floor) * row.rate)


def bracket(taxable: int, year: str, status: str) -> Decimal:
    """The marginal bracket: the rate on the top dollar of taxable income
    (the lowest rate when there is none)."""
    return _row(max(taxable, 0), year, status).rate
