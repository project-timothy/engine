"""The yearly IRS filing of a small charity (board t-990n, 2026-10-16).

Setup puts the Form 990 on a charity's calendar (``form-990`` in its
obligations file), due the 15th day of the fifth month after its year
ends. What it files depends on what it takes in: normally $50,000 or less
a year, the 990-N e-Postcard, filed online at irs.gov in a few minutes;
more, a 990-EZ or a full 990. Code, row 1 (docs/boundary-rules.md): the
dates and the threshold are arithmetic; the figure comes from the books.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

OBLIGATION_ID = "form-990"

POSTCARD_LIMIT_CENTS = 5_000_000
"""$50,000: normally this much or less a year, the 990-N is enough."""

ASKS_FOR = (
    "Employer identification number (EIN)",
    "Tax year",
    "Legal name and mailing address",
    "Any other names the ministry uses",
    "Name and address of a principal officer",
    "Website, if it has one",
    "That its gross receipts are normally $50,000 or less",
    "Whether it has closed down",
)
"""What the 990-N e-Postcard asks, in the IRS's order."""


def tax_year_of(due: date) -> tuple[str, str]:
    """The year a filing due on ``due`` covers: the twelve months ending
    the last day of the fifth month before it."""
    month, year = due.month - 5, due.year
    if month <= 0:
        month, year = month + 12, year - 1
    end = date(year, month, calendar.monthrange(year, month)[1])
    after = end + timedelta(days=1)
    return after.replace(year=after.year - 1).isoformat(), end.isoformat()


def files(gross_cents: int) -> str:
    """What a charity that took in ``gross_cents`` in the year files."""
    if gross_cents <= POSTCARD_LIMIT_CENTS:
        return (
            "the 990-N e-Postcard, filed online at irs.gov, for a charity that normally "
            "takes in $50,000 or less a year"
        )
    return (
        "a Form 990-EZ or a full Form 990: a charity that normally takes in more than "
        "$50,000 a year can't use the e-Postcard"
    )
