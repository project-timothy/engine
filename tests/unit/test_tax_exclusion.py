"""The Foreign Earned Income Exclusion in the roll-up (board chunk c39, PR 1):
the physical presence test's prorated limit (Form 2555 lines 36 to 40), the
share of costs and of half of self-employment tax tied to excluded income
(line 44), the qualified business income deduction on work done in the
United States only, and the stacking rule (the Foreign Earned Income Tax
Worksheet). The worked examples come from the 11/15 plan, section 3.5:
single, no dependents, under 65, calendar 2026, gross equals net profit."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.tax import exclusion, form1040


def _abroad(dollars: int, days: int = 365) -> exclusion.Abroad:
    return exclusion.Abroad(income=dollars * 100, qualifying_days=days)


def test_example_ii_forty_thousand_abroad_all_year_fully_excluded():
    r = form1040.roll_up("2026", "single", schedule_c_net=4000000, abroad=_abroad(40000))
    x = r.exclusion
    assert r.self_employment.tax == 565182  # the exclusion never lowers it
    assert x.limit == 13290000
    assert x.excluded == 4000000
    assert x.half_disallowed == 282591  # all of half of self-employment tax
    assert x.net == 4000000 - 282591
    assert r.adjusted_gross_income == 0
    assert r.business_deduction == 0  # the work was abroad
    assert r.income_tax == 0
    assert r.total_tax == 565182
    assert form1040.each_estimate("2026", r.total_tax) == 127166


def test_example_iii_two_hundred_days_twenty_five_thousand_abroad():
    r = form1040.roll_up("2026", "single", schedule_c_net=4000000, abroad=_abroad(25000, 200))
    x = r.exclusion
    assert x.limit == 7282920  # 132,900 x 0.548, the line 38 decimal to three places
    assert x.excluded == 2500000
    assert x.half_disallowed == 176619  # 2,825.91 x 25,000 / 40,000 [NEEDS REVIEW]
    assert x.net == 2323381
    assert r.adjusted_gross_income == 1394028
    assert r.taxable_income == 0
    assert r.income_tax == 0
    assert r.total_tax == 565182
    assert form1040.each_estimate("2026", r.total_tax) == 127166


def test_example_iii_b_the_stacking_rule():
    r = form1040.roll_up("2026", "single", schedule_c_net=8500000, abroad=_abroad(25000, 200))
    x = r.exclusion
    assert r.self_employment.earnings == 7849750
    assert r.self_employment.tax == 1201012
    assert r.half_self_employment == 600506
    assert x.half_disallowed == 176619  # 6,005.06 x 25 / 85 [NEEDS REVIEW]
    assert x.net == 2323381
    assert r.adjusted_gross_income == 5576113
    assert r.before_business_deduction == 3966113
    assert r.business_deduction == 793223  # 20% of 39,661.13; base [NEEDS REVIEW]
    assert r.taxable_income == 3172890
    assert x.stacked_on == 5496271  # taxable income with the net exclusion added back
    assert r.income_tax == 680380 - 254006  # 4,263.74
    assert r.bracket == Decimal("0.22")
    assert r.total_tax == 1627386
    assert form1040.each_estimate("2026", r.total_tax) == 366162


def test_example_iii_b_unstacked_would_be_lower():
    # The stacking rule is the easiest place to be wrong: without it the
    # same taxable income would carry 3,559.47.
    from core.tax import income

    assert income.tax_on(3172890, "2026", "single") == 355947


def test_example_iv_ten_thousand_five_hundred_abroad_all_year():
    r = form1040.roll_up("2026", "single", schedule_c_net=1050000, abroad=_abroad(10500))
    assert r.income_tax == 0
    # The plan prints 1,483.60 (15.3% at once); the form rounds lines 10
    # and 11 separately, as se.py does.
    assert r.total_tax == 148361
    assert form1040.each_estimate("2026", r.total_tax) == 33381


def test_with_no_abroad_the_roll_up_is_unchanged():
    r = form1040.roll_up("2026", "single", schedule_c_net=4000000)
    assert r.exclusion is None
    assert r.total_tax == 742693


# ---- the pieces, each one named so a reviewer's answer is a one-line change ----------------


@pytest.mark.parametrize(
    ("days", "limit"),
    [
        (365, 13290000),
        (200, 7282920),
        (0, 0),
        (1, 39870),  # 1 / 365 = 0.003 (0.00274 to three places)
    ],
)
def test_the_prorated_limit_rounds_the_line_38_decimal_to_three_places(days, limit):
    assert exclusion.limit("2026", days) == limit


def test_a_leap_year_divides_by_366():
    assert exclusion.year_days(2028) == 366
    assert exclusion.year_days(2026) == 365


def test_the_limit_caps_what_is_excluded_and_costs_shrink_with_it():
    # $150,000 earned abroad with $30,000 of costs, all year: the limit
    # excludes 132,900 of the income, and the same share of the costs
    # (132,900 / 150,000 of 30,000 = 26,580) goes with it (Publication 54,
    # chapter 5).
    a = exclusion.Abroad(income=15000000, costs=3000000, qualifying_days=365)
    r = form1040.roll_up("2026", "single", schedule_c_net=12000000, abroad=a)
    x = r.exclusion
    assert x.excluded == 13290000
    assert x.costs_disallowed == 2658000
    assert x.net == 13290000 - 2658000 - x.half_disallowed


def test_a_capped_exclusion_takes_the_business_deduction_on_home_work_only():
    # $85,000, $53,125 of it abroad, 120 qualifying days: the limit
    # (132,900 x 0.329 = 43,724.10) leaves 9,400.90 of foreign income taxed.
    # The deduction's base is the $31,875 earned at home less its own share
    # of half of self-employment tax (6,005.06 x 31,875 / 85,000 = 2,251.90).
    # Figures typed from the proving ground's second calculator, 2026-11-15;
    # the first version took half less the disallowed part here and was off.
    r = form1040.roll_up("2026", "single", schedule_c_net=8500000, abroad=_abroad(53125, 120))
    assert r.exclusion.limit == 4372410
    assert r.exclusion.half_disallowed == 308901
    assert r.business_deduction == 445197
    assert r.income_tax == 294124
    assert r.total_tax == 1495136


def test_a_qualifying_year_with_nothing_earned_abroad_excludes_nothing():
    r = form1040.roll_up(
        "2026", "single", schedule_c_net=4000000, abroad=exclusion.Abroad(0, qualifying_days=365)
    )
    assert r.exclusion.net == 0
    assert r.total_tax == 742693


def test_the_four_hundred_dollar_minimum_never_comes_from_foreign_work_alone():
    # Section 7, item 7 of the plan: read as no [NEEDS REVIEW].
    r = form1040.roll_up(
        "2026", "single", schedule_c_net=4000000, wages=3000000, abroad=_abroad(40000)
    )
    assert r.business_deduction == 0


def test_wages_earned_abroad_are_left_to_the_preparer():
    r = form1040.roll_up("2026", "single", schedule_c_net=4000000, wages=100, abroad=_abroad(40000))
    assert "the exclusion on wages earned abroad" in r.left_out
    plain = form1040.roll_up("2026", "single", schedule_c_net=4000000, abroad=_abroad(40000))
    assert "the exclusion on wages earned abroad" not in plain.left_out


@pytest.mark.parametrize(
    ("net", "abroad_days", "share"),
    [
        (1000000, 365, 1000000),
        (1000000, 0, 0),
        (3650000, 200, 2000000),
        (1051798, 359, 1034508),  # 359 of 365 days, rounded to the cent
    ],
)
def test_income_earned_abroad_is_split_by_the_days_abroad(net, abroad_days, share):
    # How the year's income divides between abroad and home when the person
    # hasn't said: evenly by day [NEEDS REVIEW].
    assert exclusion.earned_abroad(net, abroad_days, 365) == share
