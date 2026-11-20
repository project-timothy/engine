"""The pure tax calculator (board chunk c36, PR 2): self-employment tax,
income tax from the brackets, and a Form 1040 roll-up for a person with no
exclusion. The worked examples come from the 11/15 plan, section 3.5: single,
no dependents, under 65, calendar 2026, no other income, no withholding.
Each line is figured as the form does, in cents."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.tax import figures, form1040, income, se


def test_example_i_forty_thousand_in_the_united_states_all_year():
    r = form1040.roll_up("2026", "single", schedule_c_net=4000000)
    assert r.self_employment.earnings == 3694000
    assert r.self_employment.tax == 565182
    assert r.half_self_employment == 282591
    assert r.adjusted_gross_income == 3717409
    assert r.standard_deduction == 1610000
    assert r.before_business_deduction == 2107409
    assert r.business_deduction == 421482  # the smaller of 20% of 37,174.09 and of 21,074.09
    assert r.taxable_income == 1685927
    assert r.income_tax == 177511
    assert r.total_tax == 742693
    assert r.bracket == Decimal("0.12")
    assert r.balance == 742693
    assert form1040.each_estimate("2026", r.total_tax) == 167106


@pytest.mark.parametrize(
    ("net", "earnings", "tax", "half"),
    [
        (4000000, 3694000, 565182, 282591),  # (ii) and (iii): $40,000
        (8500000, 7849750, 1201012, 600506),  # (iii-b): $85,000
        # (iv): $10,500. The plan prints $1,483.60 (15.3% at once); the form
        # rounds line 10 ($1,202.40) and line 11 ($281.21) and adds them.
        (1050000, 969675, 148361, 74181),
    ],
)
def test_the_self_employment_lines_of_examples_ii_to_iv(net, earnings, tax, half):
    s = se.self_employment(net, "2026")
    assert (s.earnings, s.tax, s.half) == (earnings, tax, half)


def test_examples_ii_and_iv_estimates_from_self_employment_tax_alone():
    assert form1040.each_estimate("2026", 565182) == 127166
    assert form1040.each_estimate("2026", 148361) == 33381


def test_no_estimates_under_a_thousand_dollars_owed():
    assert form1040.each_estimate("2026", 99999) == 0
    assert form1040.each_estimate("2026", 100000) == 22500


def test_income_tax_follows_the_printed_table():
    assert income.tax_on(0, "2026", "single") == 0
    assert income.tax_on(1240000, "2026", "single") == 124000
    assert income.tax_on(5040000, "2026", "single") == 580000
    assert income.tax_on(2107409, "2026", "single") == 228089  # 1,240 + 12% of 8,674.09
    assert income.tax_on(10080000, "2026", "married_jointly") == 1160000
    assert income.tax_on(70000000, "2026", "single") == 19297925 + 2197800  # 37% of $59,400


def test_the_bracket_is_the_rate_on_the_top_dollar():
    assert income.bracket(0, "2026", "single") == Decimal("0.10")
    assert income.bracket(1240000, "2026", "single") == Decimal("0.10")
    assert income.bracket(1240001, "2026", "single") == Decimal("0.12")
    assert income.bracket(5500000, "2026", "single") == Decimal("0.22")
    assert income.bracket(5500000, "2026", "married_jointly") == Decimal("0.12")


def test_married_filing_jointly_takes_the_joint_deduction_and_table():
    r = form1040.roll_up("2026", "married_jointly", schedule_c_net=4000000)
    assert r.standard_deduction == 3220000
    assert r.before_business_deduction == 3717409 - 3220000
    assert r.bracket == Decimal("0.10")


def test_wages_add_to_income_and_withholding_comes_off_the_balance():
    r = form1040.roll_up("2026", "single", schedule_c_net=0, wages=3000000, withheld=200000)
    assert r.self_employment.tax == 0
    assert r.adjusted_gross_income == 3000000
    assert r.business_deduction == 0
    assert r.taxable_income == 1390000
    assert r.income_tax == 124000 + 18000  # 1,240 + 12% of 1,500
    assert r.balance == 142000 - 200000  # a refund is a negative balance


def test_the_four_hundred_dollar_minimum_business_deduction():
    # $1,500 net: 20% of its $1,394.03 of qualified income is $278.81, and at
    # least $1,000 of it earns the minimum (Revenue Procedure 2025-32, 2.12).
    r = form1040.roll_up("2026", "single", schedule_c_net=150000, wages=2000000)
    assert r.business_deduction == 40000
    small = form1040.roll_up("2026", "single", schedule_c_net=90000, wages=2000000)
    assert small.business_deduction < 40000


def test_above_the_threshold_the_business_deduction_is_left_for_the_preparer():
    r = form1040.roll_up("2026", "single", schedule_c_net=30000000)
    assert r.business_deduction == 0
    assert "the qualified business income deduction" in r.left_out


def test_a_status_tim_does_not_work_out_is_refused():
    with pytest.raises(ValueError, match="single or married filing jointly"):
        form1040.roll_up("2026", "head_of_household", schedule_c_net=100)


def test_a_year_with_no_figures_is_refused_in_plain_words():
    with pytest.raises(figures.NoYear):
        form1040.roll_up("2019", "single", schedule_c_net=100)


def test_the_roll_up_names_its_sources():
    r = form1040.roll_up("2026", "single", schedule_c_net=4000000)
    assert "Revenue Procedure 2025-32" in r.figures_from
    assert "Schedule SE" in r.figures_from


def test_the_packet_uses_this_calculator_and_cites_the_tables():
    from core.agents.expenses import packet

    assert packet.self_employment is se.self_employment
    assert "Revenue Procedure 2025-32" in packet.figures_from("2026")
    assert "Social Security Administration" not in packet.figures_from("2026")


# ---- wages and withholding (c38: the second calculator's two findings) -------------------
# Expected figures typed from the proving ground's second calculator
# (proving_ground/taxcheck.py, 2026-11-15), never imported.


@pytest.mark.parametrize(
    ("net", "wages", "tax"),
    [
        # Schedule SE line 9: the $184,500 base less their own W-2 wages
        # ($4,500 left), so only that much carries 12.4%.
        (5000000, 18000000, 189708),
        # Wages past the base: no Social Security on the profit at all.
        (3000000, 19000000, 80345),
    ],
)
def test_their_own_wages_come_off_the_social_security_base(net, wages, tax):
    assert se.self_employment(net, "2026", wages=wages).tax == tax
    assert (
        form1040.roll_up("2026", "single", schedule_c_net=net, wages=wages).total_tax
        == ({5000000: 4649755, 3000000: 4237890}[net])
    )


def test_a_spouses_wages_never_touch_the_self_employment_base():
    r = form1040.roll_up(
        "2026", "married_jointly", schedule_c_net=4000000, spouse_wages=5000000, withheld=300000
    )
    assert r.self_employment.tax == 565182  # as with no wages at all
    assert r.adjusted_gross_income == 8717409
    assert r.taxable_income == 4753927
    assert r.income_tax == 520871
    assert r.total_tax == 1086053
    assert form1040.each_estimate("2026", r.total_tax, withheld=r.withheld) == 169362


def test_estimates_take_ninety_percent_of_the_tax_then_the_withholding():
    # Form 1040-ES line 12a is 90% of the tax, rounded to the cent; line 13
    # takes the withholding off; a quarter each.
    r = form1040.roll_up("2026", "single", schedule_c_net=6000000, withheld=200000)
    assert r.total_tax == 1203720
    assert form1040.each_estimate("2026", r.total_tax, withheld=200000) == 220837


def test_no_estimates_when_withholding_leaves_under_a_thousand_owed():
    r = form1040.roll_up("2026", "single", schedule_c_net=3000000, withheld=420000)
    assert r.total_tax == 518132
    assert form1040.each_estimate("2026", r.total_tax, withheld=420000) == 0


def test_the_minimum_business_deduction_never_passes_taxable_income():
    # Caleb-sized: $10,517.98 with nothing taxable after the standard
    # deduction. The $400 minimum is capped at the $0.00 it would come off
    # (the owner, 2026-11-15); the second calculator does the same.
    r = form1040.roll_up("2026", "single", schedule_c_net=1051798)
    assert r.before_business_deduction == 0
    assert r.business_deduction == 0
    assert r.income_tax == 0
