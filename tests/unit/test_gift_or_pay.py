"""What a W-2 says, and gift or pay (board chunk c38, PR 2; the plan of
2026-11-15, gaps B and D, and the persona findings of Session 2).

- A W-2 from an agency that employs the person: Tim keeps its box 1 wages
  and box 2 federal income tax withheld for the year, and the packet shows
  them on the payer's row. Wages are never on Schedule C.
- A payer kept as a personal gift that looks like an organization (a church,
  a fund, a fellowship), or whose deposits come about monthly, gets one plain
  question: "Do they send this because of your ministry work?" The answer is
  kept on the payer, so it's asked once. A yes is income with no tax form,
  counted on Schedule C; a no keeps it a gift.
- Finding 3 (M04): a deposit of their own money that was a gift for them,
  from someone who isn't a payer, lands as a personal gift for that one line.
  No payer is made, so it never claims other deposits.
- Finding 1 (Y02): a payer whose form isn't kept yet says, on the packet, to
  keep what the form says once it comes, even when it differs; the packet
  checks it."""

from __future__ import annotations

import pytest

from core.agents.expenses import packet, payers
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_payers import MOBILE, PAY, PAY_CSV, _line, _paid

ASK = "Do they send this because of your ministry work?"

# ---- the two signs that a "personal gift" may be pay -------------------------------------


@pytest.mark.parametrize(
    ("name", "looks"),
    [
        ("Hope Fellowship", True),
        ("Grace Community Church", True),
        ("Harvest Ministries Inc.", True),
        ("The Lind Family Foundation", True),
        ("First Assembly of God", True),
        ("Kingdom Fund", True),
        ("Aunt Maria", False),
        ("M Porter", False),
        ("Mom", False),
        ("Chuck Lindsey", False),  # "church" isn't hiding in a name
    ],
)
def test_a_name_that_reads_as_an_organization(name, looks):
    assert payers.looks_like_org(name) is looks


@pytest.mark.parametrize(
    ("days", "recurs"),
    [
        (["2026-06-01", "2026-07-01", "2026-08-03"], True),
        (["2026-01-02", "2026-02-01", "2026-03-02", "2026-04-01"], True),
        (["2026-06-01", "2026-07-01"], False),  # two isn't a schedule
        (["2026-06-01", "2026-06-03", "2026-06-09"], False),  # three in a week isn't monthly
        (["2026-01-05", "2026-05-20", "2026-11-30"], False),  # three far apart isn't either
    ],
)
def test_deposits_that_come_about_monthly(days, recurs):
    assert payers.recurs(days) is recurs


# ---- through the door: the question, asked once ------------------------------------------

MONTHLY_CSV = """Living Water Ministry Checking ...4417
Statement period,06/01/2026 - 08/31/2026

Date,Description,Debit,Credit
06/01/2026,ZELLE FROM M PORTER 2501,,500.00
07/01/2026,ZELLE FROM M PORTER 2502,,500.00
08/03/2026,ZELLE FROM M PORTER 2503,,500.00
08/05/2026,ONLINE GIFT HOPE FELLOWSHIP,,250.00
"""


def _monthly(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, MONTHLY_CSV)
    for month in ("2026-06", "2026-07", "2026-08"):
        [me] = door.call("month_report", {"month": month})["people"]
        for b in me["bank"]:
            put = door.call(
                "explain_bank_line", {"line": b["source"], "said": "mine", "whose": "own"}
            )
            assert put["status"] == "set", put
    return door


def test_a_gift_from_an_organization_is_asked_about_when_kept(solo):  # noqa: F811
    door = _monthly(solo)
    put = door.call("keep_payers", {"payer": "Hope Fellowship", "kind": "personal gift"})
    assert put["status"] == "set" and ASK in put["say"]
    got = door.call("tax_packet", {"year": "2026"})
    assert {"payer": "Hope Fellowship", "ask": ASK} in got["asks"]
    assert [g["from"] for g in got["personal_gifts"]] == ["Hope Fellowship"]


def test_a_gift_that_comes_every_month_is_asked_about(solo):  # noqa: F811
    door = _monthly(solo)
    put = door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER"})
    assert ASK not in put["say"]  # her name says nothing; the deposits do
    got = door.call("tax_packet", {"year": "2026"})
    assert {"payer": "Mom", "ask": ASK} in got["asks"]


def test_a_no_keeps_it_a_gift_and_the_question_is_asked_once(solo):  # noqa: F811
    door = _monthly(solo)
    door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER"})
    put = door.call("keep_payers", {"payer": "Mom", "ministry_work": "no"})
    assert put["status"] == "set", put
    got = door.call("tax_packet", {"year": "2026"})
    assert not [a for a in got["asks"] if a.get("payer") == "Mom"]
    assert sum(g["from"] == "Mom" for g in got["personal_gifts"]) == 3
    assert got["schedule_c"]["income"] == "0.00"
    # The answer stays when the payer is said again.
    door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER;PORTER"})
    got = door.call("tax_packet", {"year": "2026"})
    assert not [a for a in got["asks"] if a.get("payer") == "Mom"]


def test_a_yes_is_income_with_no_tax_form(solo):  # noqa: F811
    door = _monthly(solo)
    door.call("keep_payers", {"payer": "Hope Fellowship", "kind": "personal gift"})
    put = door.call("keep_payers", {"payer": "Hope Fellowship", "ministry_work": "yes"})
    assert put["status"] == "set" and "Schedule C" in put["say"]
    got = door.call("tax_packet", {"year": "2026"})
    assert not got["personal_gifts"]
    [hope] = [p for p in got["payers"] if p["name"] == "Hope Fellowship"]
    assert hope["form"] == "" and hope["kind"] == payers.WORK_GIFT
    assert hope["came_in_seen"] == "250.00"
    assert got["schedule_c"]["income"] == "250.00"
    assert got["schedule_c"]["with_no_form"] == "250.00"
    assert not got["asks"]


def test_an_answer_needs_a_payer_on_the_list(solo):  # noqa: F811
    door = _monthly(solo)
    put = door.call("keep_payers", {"payer": "Wycliffe", "ministry_work": "yes"})
    assert put["status"] == "needs" and "Wycliffe" in put["say"]


def test_only_a_gift_is_asked_gift_or_pay(solo):  # noqa: F811
    door = _monthly(solo)
    door.call("keep_payers", {"payer": "Faith Assembly", "kind": "sending church"})
    put = door.call("keep_payers", {"payer": "Faith Assembly", "ministry_work": "yes"})
    assert put["status"] == "needs"


# ---- what a W-2 says (gap B) -------------------------------------------------------------


def _w2(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, PAY_CSV)
    door.call("keep_payers", {"payer": "EquipNet", "kind": "agency", "form": "W-2"})
    line = _line(door, PAY)["source"]
    door.call("explain_bank_line", {"line": line, "said": "my pay", "whose": "own"})
    return door


def test_a_w2s_wages_and_tax_withheld_are_kept(solo):  # noqa: F811
    door = _w2(solo)
    put = door.call(
        "keep_payers",
        {"payer": "EquipNet", "year": "2026", "form_says": "30,000.00", "withheld": "2,100.00"},
    )
    assert put["status"] == "set", put
    assert "$30,000.00" in put["say"] and "$2,100.00" in put["say"]
    got = door.call("tax_packet", {"year": "2026"})
    [equipnet] = got["payers"]
    assert equipnet["form_says"] == "30000.00" and equipnet["withheld"] == "2100.00"
    assert "not_seen" not in equipnet  # take-home pay is never the W-2's wages
    assert got["schedule_c"]["income"] == "0.00"  # wages are never on Schedule C


def test_tax_withheld_belongs_to_a_w2(solo):  # noqa: F811
    door = _paid(solo)  # EquipNet sends a 1099-NEC here
    put = door.call(
        "keep_payers",
        {"payer": "EquipNet", "year": "2026", "form_says": "1000.00", "withheld": "50.00"},
    )
    assert put["status"] == "needs" and "W-2" in put["say"]


def test_tax_withheld_is_said_with_the_wages(solo):  # noqa: F811
    door = _w2(solo)
    put = door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "withheld": "2100.00"})
    assert put["status"] == "needs" and "box 1" in put["say"]


def test_the_figures_keep_the_newest_word():
    said = [
        {"person": "d", "payer": "EquipNet", "year": "2026", "cents": "100", "withheld": "7"},
        {"person": "d", "payer": "equipnet", "year": "2026", "cents": "200"},
    ]
    assert payers.figures(said) == {("d", "equipnet", "2026"): 200}
    assert payers.withheld(said) == {}
    assert payers.withheld(said[:1]) == {("d", "equipnet", "2026"): 7}


# ---- finding 3 (M04): a gift for them on one line ---------------------------------------


def test_their_own_money_given_them_as_a_gift_lands_as_a_gift_for_that_line(solo):  # noqa: F811
    door = _paid(solo)
    line = _line(door, MOBILE)["source"]
    put = door.call(
        "explain_bank_line",
        {"line": line, "said": "my mom's birthday gift to me", "whose": "own", "gift_from": "Mom"},
    )
    assert put["status"] == "set", put
    got = door.call("tax_packet", {"year": "2026"})
    assert {"from": "Mom", "date": "2026-08-12", "dollars": "300.00"} in got["personal_gifts"]
    assert got["unmatched"] == []
    assert "Mom" not in [p["name"] for p in got["payers"]]  # no payer was made
    # It's a gift to the person, never the ministry's: no donor letter.
    letters = door.call("donor_letters", {"year": "2026"})
    assert "Mom" not in str(letters)


# ---- finding 1 (Y02): keep what the form says --------------------------------------------


def test_a_payer_whose_form_isnt_kept_says_to_keep_it(solo):  # noqa: F811
    got = _paid(solo).call("tax_packet", {"year": "2026"})
    assert "When EquipNet's 1099-NEC comes, keep what it says" in got["says"]


def test_a_form_that_says_less_is_kept_and_named(solo):  # noqa: F811
    door = _paid(solo)
    put = door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "form_says": "300.00"})
    assert put["status"] == "set"
    assert "$100.00 less than Tim saw arrive" in put["say"] and "corrected" in put["say"]
    says = door.call("tax_packet", {"year": "2026"})["says"]
    assert "keep what it says" not in says


# ---- the page ------------------------------------------------------------------------------


def test_the_page_shows_a_w2s_withholding_and_income_with_no_form():
    got = {
        "year": "2026",
        "person": "Daniel Reyes",
        "as_of": "2026-11-15",
        "payers": [
            {
                "name": "Wycliffe",
                "kind": "agency",
                "form": "W-2",
                "came_in_seen": "800.00",
                "form_says": "30000.00",
                "withheld": "2100.00",
                "deposits": [],
            },
            {
                "name": "Hope Fellowship",
                "kind": payers.WORK_GIFT,
                "form": "",
                "came_in_seen": "250.00",
                "deposits": [],
            },
        ],
        "personal_gifts": [],
        "unmatched": [],
        "asks": [{"payer": "Mom", "ask": ASK}],
        "schedule_c": {
            "income": "250.00",
            "with_no_form": "250.00",
            "costs": "0.00",
            "net": "250.00",
        },
        "says": "",
    }
    blocks = packet.page(got)
    t = {b[1]: b[2] for b in blocks if b[0] == "table"}
    rows = t["Income from each payer"]
    assert ("Its Form W-2 (wage and tax statement) says", "$30,000.00", True) in rows
    assert ("Federal income tax withheld, its box 2", "$2,100.00", False) in rows
    assert ("Hope Fellowship, a gift for their ministry work: seen arriving in the bank",
            "$250.00", True) in rows  # fmt: skip
    sc = t["Schedule C, Profit or Loss From Business"]
    assert sc[0] == (f"Income reported on {packet._FORMS['1099-NEC']}", "$0.00", False)
    assert ("Gifts for their ministry work, with no tax form", "$250.00", False) in sc
    words = " ".join(b[1] for b in blocks if b[0] in ("text", "small"))
    assert f"Mom: {ASK}" in words


def test_a_gift_deposited_by_phone_carries_no_spending_category(solo):  # noqa: F811
    """Take 5, M01: Aunt Joan's $250 check, "deposited it by phone", was filed
    under Communication: the category was guessed from her words. Money in
    never carries a spending category, guessed or passed."""
    door = _paid(solo)
    line = _line(door, MOBILE)
    said = "a check from my aunt Joan Porter, a gift to the ministry, I deposited it by phone"
    put = door.call(
        "explain_bank_line",
        {"line": line["source"], "said": said, "whose": "ministry", "gift_from": "Joan Porter"},
    )
    assert put["status"] == "set", put
    kept = _line(door, MOBILE)
    assert kept["gift_from"] == "Joan Porter" and kept["category"] is None, kept
    again = door.call(
        "explain_bank_line",
        {
            "line": line["source"],
            "said": said,
            "whose": "ministry",
            "gift_from": "Joan Porter",
            "category": "Communication",
        },
    )
    assert again["status"] == "set" and _line(door, MOBILE)["category"] is None, again
