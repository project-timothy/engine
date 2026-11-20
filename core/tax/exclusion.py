"""The Foreign Earned Income Exclusion under the physical presence test
(board chunk c39, PR 1), for a self-employed person's Schedule C income.

Each rule cites the IRS worksheet it follows:

- The limit, prorated for a part year: Form 2555 lines 36 to 39 (the
  year's most, times the qualifying days in the tax year over 365, or 366,
  as a decimal rounded to three places).
- What is excluded: the smaller of the income earned abroad and that limit
  (line 40). Housing amounts are left to the preparer.
- What comes back: the costs tied to excluded income (Publication 54,
  chapter 5) and the share of the deduction for half of self-employment tax
  tied to it (Form 2555 line 44, its instructions), so the net exclusion is
  line 43 less line 44.
- The qualified business income deduction counts only work done in the
  United States (Form 8995 instructions: income not connected with a
  United States trade or business).
- Stacking: income tax on what is left is figured at the rates it would
  have had with the excluded income still there (the Foreign Earned Income
  Tax Worksheet in the Form 1040 instructions).

The exclusion never lowers self-employment tax (Schedule SE instructions);
``se.py`` never sees it. Three readings wait for a person who prepares
missionary returns (the plan's section 7, items 5 to 7), each behind one
named function here so the answer is a one-line change: ``half_disallowed``,
``qualified_income``, and the minimum deduction staying out when
``qualified_income`` is foreign only. ``earned_abroad`` is a fourth: how the
year's income splits by day when the person hasn't said.

Pure: cents in, cents out, the yearly figures from ``tables/``."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import figures, income
from .se import cents


@dataclass(frozen=True)
class Abroad:
    """What the exclusion needs, in cents: the Schedule C income earned
    abroad inside the qualifying period, the costs that income carried, and
    the days of the qualifying 12 months that fall in the tax year (Form
    2555 line 36; 365 for a whole year)."""

    income: int
    costs: int = 0
    qualifying_days: int = 0


@dataclass(frozen=True)
class Exclusion:
    qualifying_days: int
    limit: int  # line 39
    excluded: int  # line 40: the income kept off the return
    costs_disallowed: int  # costs tied to it
    half_disallowed: int  # the share of half of self-employment tax tied to it
    net: int  # line 45: what comes off total income
    stacked_on: int = 0  # taxable income with ``net`` added back (worksheet line 3)


def year_days(year: int) -> int:
    """365, or 366 in a leap year (Form 2555 line 37's divisor)."""
    return 366 if calendar.isleap(year) else 365


def limit(year: str, qualifying_days: int) -> int:
    """The year's most, prorated: Form 2555 line 38's decimal, rounded to
    three places, times line 37 (line 39)."""
    whole = year_days(int(year))
    days = min(max(qualifying_days, 0), whole)
    share = (Decimal(days) / whole).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return cents(Decimal(figures.year(year).cents("exclusion.most")) * share)


def earned_abroad(net: int, abroad_days: int, whole: int) -> int:
    """The share of ``net`` earned abroad when the person hasn't said how it
    splits: evenly by day, ``abroad_days`` of ``whole`` [NEEDS REVIEW: the
    plan's split, not yet read by a preparer]."""
    if whole <= 0:
        return 0
    return cents(Decimal(net) * min(max(abroad_days, 0), whole) / whole)


def half_disallowed(half: int, excluded_net: int, schedule_c_net: int) -> int:
    """The share of the deduction for half of self-employment tax tied to
    excluded income (Form 2555 line 44): excluded income over all
    self-employment income [NEEDS REVIEW, plan section 7 item 5]."""
    if schedule_c_net <= 0 or excluded_net <= 0:
        return 0
    return cents(Decimal(half) * min(excluded_net, schedule_c_net) / schedule_c_net)


def qualified_income(united_states_net: int, half: int, schedule_c_net: int) -> int:
    """The base of the qualified business income deduction when part of the
    work was abroad: the United States income less its own share of half of
    self-employment tax, United States income over all of it [NEEDS REVIEW,
    plan section 7 item 6]. With none in the United States it is nothing, so
    the $400 minimum never comes from foreign work alone (item 7, read as
    no)."""
    if united_states_net <= 0 or schedule_c_net <= 0:
        return 0
    share = min(united_states_net, schedule_c_net)
    return united_states_net - cents(Decimal(half) * share / schedule_c_net)


def work_out(year: str, abroad: Abroad, schedule_c_net: int, half: int) -> Exclusion:
    """Form 2555's lines for ``abroad`` (before stacking)."""
    cap = limit(year, abroad.qualifying_days)
    gross = max(abroad.income, 0)
    excluded = min(gross, cap)
    costs = cents(Decimal(max(abroad.costs, 0)) * excluded / gross) if gross else 0
    disallowed = half_disallowed(half, excluded - costs, schedule_c_net)
    return Exclusion(
        qualifying_days=abroad.qualifying_days,
        limit=cap,
        excluded=excluded,
        costs_disallowed=costs,
        half_disallowed=disallowed,
        net=max(excluded - costs - disallowed, 0),
    )


def stacked_tax(taxable: int, net: int, year: str, status: str) -> tuple[int, int]:
    """Income tax on ``taxable`` by the Foreign Earned Income Tax Worksheet:
    the tax on ``taxable`` plus the net exclusion, less the tax on the net
    exclusion alone. Returns the tax and the stacked income it was figured
    on (none when nothing is taxable)."""
    if taxable <= 0:
        return 0, 0
    stacked = taxable + max(net, 0)
    return income.tax_on(stacked, year, status) - income.tax_on(max(net, 0), year, status), stacked
