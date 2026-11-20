"""A Form 1040 roll-up for one person (board chunk c36; the Foreign Earned
Income Exclusion, c39, in ``exclusion.py``):
Schedule C net profit, the person's own wages and a spouse's as said,
self-employment tax, the deduction for half of it, adjusted gross income,
the standard deduction, the qualified business income deduction in its
simple form (Form 8995), taxable income, income tax from the brackets,
total tax, withholding, and the balance. Federal only, single or married filing jointly (the owner,
2026-11-15). A planning estimate for the person and their preparer; what it
leaves out it names in ``left_out``."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from . import estimates, figures, income
from . import exclusion as foreign
from .se import SelfEmployment, cents, self_employment

ALWAYS_LEFT_OUT = (
    "state income tax",
    "itemized deductions",
    "credits, like the child tax credit, the earned income credit, and health insurance "
    "credits, which lower the tax",
    "the minister's housing allowance, and Form 4361, the opt-out from self-employment tax",
    "the bona fide residence test (Tim works out the physical presence test only)",
    "the foreign tax credit and the foreign housing exclusion",
    "a spouse's self-employment",
    "retirement contributions",
)
"""What the roll-up never works out; the preparer adds these."""


@dataclass(frozen=True)
class Return:
    year: str
    status: str
    schedule_c_net: int
    wages: int  # the person's own W-2 wages
    spouse_wages: int
    self_employment: SelfEmployment
    half_self_employment: int
    adjusted_gross_income: int
    standard_deduction: int
    before_business_deduction: int
    business_deduction: int
    taxable_income: int
    income_tax: int
    bracket: Decimal
    total_tax: int
    withheld: int
    balance: int  # owed; a refund is negative
    figures_from: str
    left_out: tuple[str, ...] = field(default=ALWAYS_LEFT_OUT)
    exclusion: foreign.Exclusion | None = None  # None: worked out without it


def business_deduction(qualified: int, before: int, year: str, status: str) -> int | None:
    """The qualified business income deduction, the simple way (Form 8995):
    the smaller of 20% of qualified business income and 20% of taxable
    income before it, never under the $400 minimum once qualified income
    reaches $1,000, and never more than the taxable income it comes off.
    None above the year's threshold, where the longer form applies and the
    preparer works it out."""
    y = figures.year(year)
    if before > y.cents(f"qualified_business_income.threshold_{status}"):
        return None
    share = y.rate("qualified_business_income.share")
    found = min(cents(Decimal(max(qualified, 0)) * share), cents(Decimal(max(before, 0)) * share))
    if qualified >= y.cents("qualified_business_income.minimum_income"):
        found = max(found, y.cents("qualified_business_income.minimum_deduction"))
    return min(found, max(before, 0))  # never more than it comes off (the owner, 2026-11-15)


def roll_up(
    year: str,
    status: str,
    *,
    schedule_c_net: int,
    wages: int = 0,
    spouse_wages: int = 0,
    withheld: int = 0,
    abroad: foreign.Abroad | None = None,
) -> Return:
    """The year's federal return in cents for one person. ``wages`` are the
    person's own (they lower the self-employment base); a spouse's wages, on
    a joint return, count as income and nothing else. With ``abroad`` the
    Foreign Earned Income Exclusion is taken on the Schedule C income earned
    abroad (``exclusion.py``); without it, none is."""
    if status not in figures.STATUSES:
        raise ValueError("Tim works out single or married filing jointly only.")
    y = figures.year(year)
    se = self_employment(schedule_c_net, year, wages=wages)
    left_out = ALWAYS_LEFT_OUT
    ex, qualified = None, schedule_c_net - se.half
    if abroad is not None:
        ex = foreign.work_out(year, abroad, schedule_c_net, se.half)
        at_home = schedule_c_net - (abroad.income - abroad.costs)
        qualified = foreign.qualified_income(at_home, se.half, schedule_c_net)
        if wages:
            left_out += ("the exclusion on wages earned abroad",)
    agi = schedule_c_net + wages + spouse_wages - (ex.net if ex else 0) - se.half
    standard = y.cents(f"standard_deduction.{status}")
    before = max(agi - standard, 0)
    found = business_deduction(qualified, before, year, status)
    if found is None:
        left_out += ("the qualified business income deduction",)
        found = 0
    taxable = max(before - found, 0)
    if ex is not None:
        tax, stacked = foreign.stacked_tax(taxable, ex.net, year, status)
        ex = replace(ex, stacked_on=stacked)
    else:
        tax, stacked = income.tax_on(taxable, year, status), taxable
    total = tax + se.tax
    return Return(
        year=year,
        status=status,
        schedule_c_net=schedule_c_net,
        wages=wages,
        spouse_wages=spouse_wages,
        self_employment=se,
        half_self_employment=se.half,
        adjusted_gross_income=agi,
        standard_deduction=standard,
        before_business_deduction=before,
        business_deduction=found,
        taxable_income=taxable,
        income_tax=tax,
        bracket=income.bracket(stacked, year, status),
        total_tax=total,
        withheld=withheld,
        balance=total - withheld,
        figures_from=y.cited(
            f"brackets.{status}",
            "self_employment.wage_base",
            "self_employment.earnings_share",
            "qualified_business_income.share",
        ),
        left_out=left_out,
        exclusion=ex,
    )


each_estimate = estimates.each
"""Each of the four estimated payments (moved to ``estimates.py``, c39 PR 2)."""
