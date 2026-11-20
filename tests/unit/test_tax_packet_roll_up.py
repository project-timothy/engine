"""The packet ends in a bracket (board chunk c39, PR 3): the federal return
worked out from what Tim saw and what the person said, without the Foreign
Earned Income Exclusion always and with it beside when their travel dates
qualify, never chosen for them; next year's four estimates; and what Tim
left to the preparer. The arithmetic is ``core/tax``'s; these check the
packet reads it right."""

from __future__ import annotations

from datetime import date

from core.agents.expenses import packet

AFTER = date(2027, 1, 20)  # the year is over: the January packet
ABROAD_ALL_YEAR = [{"left": "2025-12-30", "back": "2027-01-02", "country": "Nepal"}]


def _pay(dollars: str, form: str = "1099-NEC", **more) -> dict:
    return {"form": form, "came_in_seen": dollars, "form_says": dollars, **more}


def _taxes(payers, told, today=AFTER, rows=()):
    says: list[str] = []
    got = packet.taxes("2026", payers, list(rows), says, told, today)
    return got, " ".join(says)


def test_abroad_all_year_shows_the_tax_both_ways_and_never_picks():
    told = {"filing_status": "single", "trips": ABROAD_ALL_YEAR, "estimates_paid": []}
    got, says = _taxes([_pay("40000.00")], told)
    f = got["federal_tax"]
    plain, taken = f["without_exclusion"], f["with_exclusion"]
    # Example (i): the same $40,000 with no exclusion.
    assert plain["income_tax"] == "1775.11" and plain["top_bracket"] == "12"
    assert plain["total_tax"] == "7426.93"
    assert [e["dollars"] for e in plain["estimates"]] == ["1671.06"] * 4
    # Example (ii): all of it excluded; self-employment tax is all that's left.
    assert taken["excluded"] == "37174.09"
    assert taken["income_tax"] == "0.00" and taken["top_bracket"] == ""
    assert taken["total_tax"] == "5651.82"
    assert [e["dollars"] for e in taken["estimates"]] == ["1271.66"] * 4
    assert "part_year_through" not in f
    assert "The choice is theirs" in says
    assert "top bracket 12 percent" in says


def test_with_no_travel_dates_the_return_is_worked_out_without_it_and_says_why():
    got, says = _taxes([_pay("40000.00")], {"filing_status": "single"})
    assert "with_exclusion" not in got["federal_tax"]
    assert "With no travel dates said, it's worked out without the Foreign Earned" in says


def test_travel_dates_short_of_330_days_say_so():
    told = {"filing_status": "single", "trips": [{"left": "2026-03-01", "back": "2026-06-01"}]}
    got, says = _taxes([_pay("40000.00")], told)
    assert "with_exclusion" not in got["federal_tax"]
    assert "fall short of the 330 full days" in says


def test_their_own_w2_wages_lower_the_self_employment_base():
    payers = [_pay("180000.00", "W-2", withheld="20000.00"), _pay("50000.00")]
    got, _ = _taxes(payers, {"filing_status": "single"})
    assert got["self_employment_tax"]["tax"] == "1897.08"  # base $4,500 after the wages
    assert got["federal_tax"]["without_exclusion"]["withheld"] == "20000.00"


def test_a_joint_return_adds_the_spouses_wages_and_withholding():
    told = {
        "filing_status": "married filing jointly",
        "spouse": {"wages": "50000.00", "withheld": "3000.00"},
    }
    got, _ = _taxes([_pay("40000.00")], told)
    v = got["federal_tax"]["without_exclusion"]
    assert got["federal_tax"]["filing_status"] == "married_jointly"
    assert v["standard_deduction"] == "32200.00"
    assert v["total_tax"] == "10860.53"  # as the calculator's joint case
    assert v["withheld"] == "3000.00"


def test_a_status_not_said_is_worked_out_as_single_and_asked():
    got, says = _taxes([_pay("40000.00")], {})
    assert got["federal_tax"]["filing_status"] == "single"
    assert "isn't said, so this is worked out as single" in says


def test_estimates_already_paid_show_against_the_year():
    told = {
        "filing_status": "single",
        "estimates_paid": [{"date": "2026-04-15", "dollars": "1000.00"}],
    }
    got, says = _taxes([_pay("40000.00")], told)
    v = got["federal_tax"]["without_exclusion"]
    assert v["estimates_paid"] == "1000.00"
    assert v["balance"] == "6426.93"
    assert "$1,000.00 in estimates already paid toward 2026" in says


def test_asked_mid_year_it_is_a_part_year_figure():
    got, says = _taxes([_pay("10000.00")], {"filing_status": "single"}, today=date(2026, 8, 1))
    assert got["federal_tax"]["part_year_through"] == "2026-08-01"
    assert "a part-year figure" in says


def test_the_page_ends_in_the_bracket_and_the_preparers_list():
    told = {"filing_status": "single", "trips": ABROAD_ALL_YEAR}
    got, _ = _taxes([_pay("40000.00")], told)
    got.update(
        year="2026",
        person="Eli Porter",
        as_of="2027-01-20",
        payers=[],
        unmatched=[],
        asks=[],
        personal_gifts=[],
    )
    blocks = packet.page(got)
    tables = {b[1]: b[2] for b in blocks if b[0] == "table"}
    plain = tables["Federal tax for 2026, Form 1040, without the Foreign Earned Income Exclusion"]
    taken = tables["Federal tax for 2026, Form 1040, with the Foreign Earned Income Exclusion"]
    assert ("Top bracket", "12 percent", False) in plain
    assert ("Top bracket", "none", False) in taken
    assert ("Total federal tax", "$5,651.82", True) in taken
    assert "Estimated tax payments for 2027, Form 1040-ES, with the exclusion" in tables
    assert ("State income tax", "", False) in tables["What Tim left to your preparer"]
    words = " ".join(str(b[1]) for b in blocks if b[0] == "text")
    assert "The choice is theirs" in words
    assert "self-employment tax only" not in words
