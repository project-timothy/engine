"""Estimated tax payments under the safe-harbor rules (board chunk c39, PR 2),
by the Form 1040-ES worksheet: line 12a is 90% of the tax expected this year,
line 12b is last year's total tax (110% of it when last year's adjusted gross
income passed $150,000), line 12c the smaller, line 13 the withholding
expected, and nothing is required when under $1,000 would be owed after
withholding (line 14b). Each payment is a quarter of line 14a."""

from __future__ import annotations

from core.tax import estimates, form1040


def test_ninety_percent_of_this_years_tax_when_last_year_is_not_known():
    p = estimates.plan("2026", 742693)
    assert p.required == 668424  # 90% of 7,426.93, to the cent
    assert p.each == 167106  # example (i)
    assert p.by == estimates.THIS_YEAR


def test_last_years_tax_is_the_safe_harbor_when_it_is_smaller():
    p = estimates.plan("2026", 742693, last_year_tax=400000)
    assert p.required == 400000
    assert p.each == 100000
    assert p.by == estimates.LAST_YEAR


def test_a_hundred_and_ten_percent_of_last_year_above_a_hundred_fifty_thousand():
    p = estimates.plan("2026", 3000000, last_year_tax=1000000, last_year_agi=15000001)
    assert p.required == 1100000
    at_the_line = estimates.plan("2026", 3000000, last_year_tax=1000000, last_year_agi=15000000)
    assert at_the_line.required == 1000000


def test_withholding_comes_off_line_twelve_c():
    p = estimates.plan("2026", 1203720, withheld=200000)
    assert p.required == 883348  # 10,833.48 less 2,000.00
    assert p.each == 220837


def test_nothing_required_under_a_thousand_owed_after_withholding():
    assert estimates.plan("2026", 99999).each == 0
    assert estimates.plan("2026", 518132, withheld=420000).each == 0
    assert estimates.plan("2026", 100000).each == 22500


def test_no_tax_last_year_means_none_required():
    # Line 12b can be zero: a person who owed nothing last year (and was a
    # citizen all year) owes no estimates this year.
    p = estimates.plan("2026", 742693, last_year_tax=0)
    assert p.required == 0 and p.each == 0
    assert p.by == estimates.LAST_YEAR


def test_payments_already_made_are_shown_against_the_year():
    p = estimates.plan("2026", 742693, paid=[("2026-04-15", 167106), ("2026-06-15", 100000)])
    assert p.paid == 267106
    assert p.left == 668424 - 267106
    over = estimates.plan("2026", 100000, paid=[("2026-04-15", 500000)])
    assert over.left == 0


def test_the_four_payments_carry_the_odd_cents_on_the_first():
    p = estimates.plan("2026", 111113)  # 90% = 1,000.02; a quarter is 250.005
    assert p.each == 25001
    assert estimates.four(100002) == [25002, 25000, 25000, 25000]


def test_form1040_each_estimate_is_the_same_rule():
    assert form1040.each_estimate is estimates.each
    assert estimates.each("2026", 565182) == 127166  # example (ii)
