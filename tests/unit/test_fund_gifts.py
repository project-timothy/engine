"""A gift to the ministry given toward a missionary it supports, the fund as
a view, the family wall, and the church's 1099 total as pay only (board
chunk c34 PR 3; the 11/15 plan, Session 5).

A church that sends its own missionary holds a fund for them: gifts given
to the church toward that missionary, less what it paid them as pay and
paid them back. The gift stays the church's income and its donor's gift,
so the fund is a view over the books, never an account of its own (the
owner's call of 2026-11-14). A gift set aside for a specific person isn't a
charitable gift, and IRS Publication 526's own example is a parent paying a
missionary child's costs: a giver who is the missionary's family can't tag
a gift toward their fund. The church's 1099 total reads only what it paid
as pay (``church_pay.by_person``), each person at or over the year's line
needing a 1099-NEC."""

from __future__ import annotations

from pathlib import Path

from core.agents.expenses import church_pay, export
from core.agents.expenses import statements as st
from core.tax import forms
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_pay_day import _line, _set_pay
from tests.unit.test_statements import MINISTRY_CSV

HOLLIS_DAY = "2026-08-03"


def _rows() -> list[dict]:
    return [
        {"date": "2026-01-05", "dollars": "500.00", "role": st.CAME_IN, "for_fund": "ruth"},
        {"date": "2026-02-05", "dollars": "300.00", "role": st.CAME_IN, "for_fund": "ruth"},
        {"date": "2026-02-06", "dollars": "900.00", "role": st.CAME_IN},  # the church's own
        {"date": "2025-12-05", "dollars": "700.00", "role": st.CAME_IN, "for_fund": "ruth"},
        {"date": "2026-03-12", "dollars": "-400.00", "role": st.PAY_OUT, "paid_to": "ruth"},
        {"date": "2026-03-20", "dollars": "50.00", "role": st.PAID_BACK, "paid_to": "ruth"},
        {"date": "2026-04-20", "dollars": "25.00", "role": st.PAID_BACK, "paid_to": "Ruth Hollis"},
        {"date": "2026-03-12", "dollars": "-250.00", "role": st.PAY_OUT, "paid_to": "abe"},
    ]


# ---- the fund as a view ------------------------------------------------------------------


def test_the_fund_is_tagged_gifts_less_pay_and_what_was_paid_back():
    named = {"ruth": "Ruth Hollis"}.get
    assert church_pay.funds(_rows(), "2026", lambda who: named(who, who)) == {
        "ruth": {"gifts": "800.00", "pay": "400.00", "paid_back": "75.00", "left": "325.00"}
    }


def test_no_tagged_gift_means_no_fund():
    untagged = [r for r in _rows() if not r.get("for_fund")]
    assert church_pay.funds(untagged, "2026") == {}


# ---- the 1099 line -------------------------------------------------------------------------


def test_the_1099_nec_line_comes_from_the_years_table():
    assert forms.needs_1099_nec(200000, "2026") is True
    assert forms.needs_1099_nec(199999, "2026") is False
    assert forms.needs_1099_nec(500000, "1999") is None  # no table held: never guessed


def test_the_filings_carry_pay_by_person_only_where_pay_was_paid():
    year = {"Income:Gifts": -500000, "Expenses:Pay": 225000}
    pay = church_pay.by_person(_rows(), "2026")
    f = export.filings(year, {}, "Pay", pay=pay, year="2026")
    assert f["pay_by_person"] == {
        "abe": {"pay": "250.00", "needs_1099_nec": False},
        "ruth": {"pay": "400.00", "needs_1099_nec": False},
    }
    assert "pay_by_person" not in export.filings(year, {}, "Pay")


# ---- through the door ----------------------------------------------------------------------


def _tag(door, **args) -> dict:
    return door.call(
        "explain_bank_line",
        {
            "line": _line(door, HOLLIS_DAY),
            "said": "a gift toward Daniel's support",
            "gift_from": "Hollis Family",
            "for_fund": "Daniel Reyes",
            **args,
        },
    )


def _family(solo, kin: str) -> None:  # noqa: F811
    path = Path(solo["root"]) / "living-water" / "authority.toml"
    text = path.read_text()
    path.write_text(
        text.replace("[people.daniel-reyes]\n", f'[people.daniel-reyes]\nfamily = ["{kin}"]\n', 1)
    )


def test_a_gift_toward_their_fund_stays_the_ministrys_gift(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    put = _tag(daniel)
    assert put["status"] == "set", put
    assert "toward Daniel Reyes's fund" in put["say"]
    row = next(
        b
        for p in daniel.call("month_report", {"month": "2026-08"})["people"]
        for b in p["bank"]
        if b["date"] == HOLLIS_DAY
    )
    assert row["role"] == st.CAME_IN and row["gift_from"] == "Hollis Family"
    assert row["for_fund"] == "daniel-reyes"


def test_the_tag_needs_a_giver_money_in_and_someone_in_the_ministry(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    assert _tag(daniel, gift_from="")["status"] == "asks"
    assert _tag(daniel, for_fund="Nobody We Know")["status"] == "asks"
    out = daniel.call(
        "explain_bank_line",
        {
            "line": _line(daniel, "2026-08-20"),
            "said": "rooms",
            "gift_from": "Hollis Family",
            "for_fund": "Daniel Reyes",
        },
    )
    assert out["status"] == "asks"


def test_family_cant_give_toward_their_fund(solo):  # noqa: F811
    _family(solo, "linda-park")
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    out = _tag(daniel, gift_from="Linda Park")
    assert out["status"] == "refused", out
    assert "Publication 526" in out["say"] and "whose own" in out["say"]
    assert _tag(daniel, gift_from="Daniel's mother")["status"] == "refused"
    assert _tag(daniel, gift_from="Daniel Reyes")["status"] == "refused"  # their own money


def test_a_shared_last_name_asks_once(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    out = _tag(daniel, gift_from="Maria Reyes")
    assert out["status"] == "asks" and "family" in out["say"]
    assert _tag(daniel, gift_from="Maria Reyes", not_family=True)["status"] == "set"


def test_the_accountant_sees_the_fund_and_the_pay_1099_total(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    assert _tag(daniel)["status"] == "set"
    paid = daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "his pay", "pay_of": "Daniel Reyes"},
    )
    assert paid["status"] == "set", paid
    linda = _door(solo, "linda-park")
    done = linda.call("cpa_export", {"year": "2026"})
    assert done["matches_the_books"], done["says"]
    assert done["filings"]["pay_by_person"] == {
        "daniel-reyes": {"pay": "86.40", "needs_1099_nec": False}
    }
    assert done["funds"] == {
        "daniel-reyes": {"gifts": "250.00", "pay": "86.40", "paid_back": "0.00", "left": "163.60"}
    }
    assert "Daniel Reyes was paid $86.40 as pay" in done["says"]


def test_a_ministry_with_no_pay_and_no_tags_exports_as_before(solo):  # noqa: F811
    """Teaching to Transform's shape: no pay resolution, nothing new shows."""
    _keep_ministry(solo, MINISTRY_CSV)
    done = _door(solo, "linda-park").call("cpa_export", {"year": "2026"})
    assert "funds" not in done and "pay_by_person" not in done["filings"]
    assert "as pay" not in done["says"]
