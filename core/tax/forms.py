"""Which information returns a payment needs (board chunk c34 PR 3): the
1099-NEC line from the year's table, never a number written here."""

from __future__ import annotations

from . import figures


def needs_1099_nec(cents: int, year: str) -> bool | None:
    """Whether a year's pay of ``cents`` to one person needs a 1099-NEC;
    None when Tim holds no figures for that year."""
    try:
        line = figures.year(year).cents("form_1099_nec.threshold")
    except figures.NoYear:
        return None
    return cents >= line
