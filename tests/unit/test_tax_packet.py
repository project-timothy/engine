"""The missionary's tax packet, its arithmetic (board chunk c33, PR 2).

Self-employment tax is the Social Security and Medicare a self-employed
person pays both halves of: 15.3% of 92.35% of their net earnings, the
Social Security part only up to the year's wage base, and none below $400
of earnings. The Foreign Earned Income Exclusion never lowers it. The day
count is drafted from the days the bank shows money spent in another
currency; Tim can't confirm the 330 days without the person's own travel
dates, so it is a draft. The four estimate dates fall on the 15th of April,
June, September, and the next January, moved past a weekend or a federal
holiday."""

from __future__ import annotations

from datetime import date

import pytest

from core.agents.expenses import packet


def test_self_employment_tax_on_caleb_sized_earnings():
    se = packet.self_employment(1051798, "2026")
    assert se.earnings == 971335  # 92.35% of $10,517.98
    assert se.tax == 148615  # 12.4% + 2.9%, each rounded as the form does: $1,486.15
    assert se.half == 74308  # the half that's deducted from income


def test_no_self_employment_tax_below_400_of_earnings():
    assert packet.self_employment(43000, "2026").tax == 0  # $430 net, $397.11 earnings
    assert packet.self_employment(43400, "2026").tax > 0


def test_social_security_stops_at_the_wage_base():
    se = packet.self_employment(30000000, "2026")  # $300,000 net
    assert se.tax == 3091245  # 12.4% of $184,500, then 2.9% of all $277,050


def test_a_year_tim_has_no_figures_for_says_so():
    with pytest.raises(KeyError):
        packet.self_employment(100000, "2019")


@pytest.mark.parametrize(
    ("year", "dates"),
    [
        (
            "2027",
            [date(2027, 4, 15), date(2027, 6, 15), date(2027, 9, 15), date(2028, 1, 18)],
        ),
        # April 15, 2028 is a Saturday, and Emancipation Day (the 16th, a
        # Sunday) is kept on Monday the 17th: the first estimate is due the 18th.
        (
            "2028",
            [date(2028, 4, 18), date(2028, 6, 15), date(2028, 9, 15), date(2029, 1, 16)],
        ),
    ],
)
def test_the_four_estimate_dates(year, dates):
    assert packet.estimate_dates(year) == dates


# ---- the days abroad, drafted from the bank --------------------------------------------


def _row(day: str, words: str, gave: str = "") -> dict:
    return {"date": day, "words": words, **({"gave": gave} if gave else {})}


ROWS = [
    _row(
        "2026-01-05",
        "NON-CHASE ATM WITHDRAW 1 HBL CHARI NP Rupee 20000.00 X 0.0065",
        "NPR 20,000.00",
    ),
    _row("2026-01-05", "NON-CHASE ATM FEE-WITH"),
    _row("2026-02-10", "PACIFIC ONEHEALTH DELHI 02/09 IN Rupee 2500.00 X 0.01058000 (EXCHG RTE)"),
    _row(
        "2026-03-30",
        "NON-CHASE ATM WITHDRAW 2 HBL CHARI NP Rupee 30000.00 X 0.0065",
        "NPR 30,000.00",
    ),
    _row("2026-04-02", "ORIG CO NAME:EQUIPNET/ GIVING ... PAYROLL"),
]


def test_a_day_with_spending_in_another_currency_counts_as_abroad():
    days = packet.foreign_days(ROWS)
    assert days == {"2026-01-05": "NP", "2026-02-10": "IN", "2026-03-30": "NP"}


def test_the_day_count_is_a_draft_with_its_long_gaps_named():
    got = packet.days_abroad(ROWS, "2026")
    assert got["days_with_spending_abroad"] == 3
    assert got["first"] == "2026-01-05" and got["last"] == "2026-03-30"
    assert got["countries"] == ["IN", "NP"]
    assert got["gaps"] == [
        {"from": "2026-01-05", "to": "2026-02-10", "days": 36},
        {"from": "2026-02-10", "to": "2026-03-30", "days": 48},
    ]
    assert got["needs"] == 330
    assert "travel dates" in got["says"]


# ---- the page and the PDF (engine tax-packet) ------------------------------------------

GOT = {
    "year": "2026",
    "person": "Daniel Reyes",
    "as_of": "2026-11-13",
    "payers": [
        {
            "name": "EquipNet",
            "kind": "support platform",
            "form": "1099-NEC",
            "came_in_seen": "1000.00",
            "form_says": "1200.00",
            "not_seen": "200.00",
            "deposits": [{"date": "2026-08-03", "dollars": "1000.00"}],
        }
    ],
    "personal_gifts": [{"from": "Aunt Maria", "date": "2026-08-09", "dollars": "100.00"}],
    "unmatched": [{"date": "2026-08-12", "dollars": "300.00", "words": "MOBILE DEPOSIT"}],
    "asks": [],
    "schedule_c": {"income": "1200.00", "costs": "0.00", "net": "1200.00"},
    "self_employment_tax": {
        "on": "1108.20",
        "tax": "169.56",
        "half_off_income": "84.78",
        "figures_from": "IRS Revenue Procedure 2025-32",
    },
    "days_abroad": {
        "days_with_spending_abroad": 18,
        "first": "2026-05-20",
        "last": "2026-10-23",
        "countries": ["IN", "NP"],
        "gaps": [{"from": "2026-06-09", "to": "2026-08-24", "days": 76}],
        "needs": 330,
        "says": "The bank shows spending abroad on 18 days in 2026.",
    },
    "says": "2026: EquipNet paid $1,000.00 that Tim saw arrive.",
}


# c39: the federal return, worked out by the packet itself on the same inputs.
GOT["federal_tax"] = packet.taxes(
    "2026", GOT["payers"], [], [], {"filing_status": "single"}, date(2026, 11, 13)
)["federal_tax"]


def _tables(blocks) -> dict[str, list]:
    return {b[1]: b[2] for b in blocks if b[0] == "table"}


def test_the_page_has_its_five_sections_in_order():
    blocks = packet.page(GOT)
    assert blocks[0] == ("title", "2026 tax packet for Daniel Reyes")
    heads = [b[1] for b in blocks if b[0] == "table"]
    assert heads == [
        "Income from each payer",
        "Schedule C, Profit or Loss From Business",
        "Self-employment tax, Schedule SE (Social Security and Medicare)",
        "Days abroad, a draft for the Foreign Earned Income Exclusion",
        "Federal tax for 2026, Form 1040",
        "Estimated tax payments for 2027, Form 1040-ES",
        "What Tim left to your preparer",
    ]


def test_the_page_says_each_figure_in_dollars():
    t = _tables(packet.page(GOT))
    assert t["Income from each payer"] == [
        ("EquipNet, a support platform: seen arriving in the bank", "$1,000.00", False),
        ("Its Form 1099-NEC (nonemployee compensation) says", "$1,200.00", True),
        ("Paid to an account Tim does not see", "$200.00", False),
    ]
    assert t["Schedule C, Profit or Loss From Business"][-1] == ("Net profit", "$1,200.00", True)
    se = t["Self-employment tax, Schedule SE (Social Security and Medicare)"]
    assert ("Self-employment tax", "$169.56", True) in se
    days = t["Days abroad, a draft for the Foreign Earned Income Exclusion"]
    assert days[0] == ("Days the bank shows spending abroad", "18", False)
    assert ("No spending abroad from June 9, 2026 to August 24, 2026", "76 days", False) in days
    f = t["Federal tax for 2026, Form 1040"]
    assert ("Federal income tax", "$0.00", False) in f
    assert ("Top bracket", "none", False) in f
    assert ("Total federal tax", "$169.56", True) in f
    # Under $1,000 owed, so none are needed (c39; before, it split the
    # self-employment tax four ways regardless).
    est = t["Estimated tax payments for 2027, Form 1040-ES"]
    assert est[0] == ("Due April 15, 2027", "$0.00", False)
    assert est[-1] == ("Total", "$0.00", True)


def test_the_page_names_what_to_ask_and_what_it_leaves_out():
    words = " ".join(b[1] for b in packet.page(GOT) if b[0] in ("text", "small"))
    assert "$300.00 on August 12, 2026 (MOBILE DEPOSIT)" in words
    assert "Personal gifts" in words and "$100.00" in words
    assert "never lowers" in words
    assert "a part-year figure" in words  # asked on November 13
    assert "self-employment tax only" not in words  # c39: the estimates carry the whole tax
    assert "Revenue Procedure 2025-32" in words
    assert "—" not in words


def test_a_year_without_figures_leaves_the_tax_sections_out():
    got = {k: v for k, v in GOT.items() if k not in ("self_employment_tax", "federal_tax")}
    got.pop("days_abroad")
    heads = [b[1] for b in packet.page(got) if b[0] == "table"]
    assert heads == ["Income from each payer", "Schedule C, Profit or Loss From Business"]
