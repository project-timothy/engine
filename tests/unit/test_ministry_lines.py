"""A ministry's month card reads clean, and the reviewer places a receipt by
asking (the owner's decisions of 2026-10-13).

A line shows its fund and a category taken from the person's own words, in
code ("the bus to Pokhara" is Travel); words that point nowhere, or two ways,
read "Uncategorized". The reviewer, or the person on their own receipts,
moves a receipt by asking their own AI. A category moves no money, so the
word decides; the change is recorded with who said it, and a month waiting
for its yes is raised again showing it. Until the sending organization's own
chart arrives, every ministry starts from one list in its own config."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.agents.expenses.categories import canonical, from_words
from core.agents.expenses.jobs import REVIEW_CARD
from core.engine.runner import run
from core.tools import catalog
from core.tools.receipts import Claim, ReceiptDesk, Receipts, take_photo
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_missionary_month import NPR_OCT_12, feed  # noqa: F401  (fixture)

STARTER = [
    "Travel",
    "Lodging",
    "Meals",
    "Ministry Supplies",
    "Communication",
    "Medical",
    "Housing",
    "Other",
]


# ---- the words ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "category"),
    [
        ("4,300 rupees for the bus to Pokhara", "Travel"),
        ("$43 at the pharmacy", "Medical"),
        ("1,250 meticais for Bibles", "Ministry Supplies"),
        ("put it under communication", "Communication"),
        ("lunch at the hotel", ""),  # two ways: the reviewer places it
        ("here's a $9 receipt", ""),
        ("printer ink for the school", "Ministry Supplies"),  # the 10/14 practice run
        ("paper and pens for class", "Ministry Supplies"),
    ],
)
def test_the_category_comes_from_the_persons_own_words(said, category):
    assert from_words(said, STARTER) == category


def test_a_category_the_ministry_does_not_keep_is_never_chosen():
    assert from_words("the bus to Pokhara", ["Meals", "Other"]) == ""


def test_a_category_is_named_the_ministrys_own_way():
    assert canonical("travel", STARTER) == "Travel"
    assert canonical("uncategorized", STARTER) == ""
    assert canonical("Software", STARTER) is None


def test_a_ministry_starts_from_the_starter_list(solo):  # noqa: F811
    text = (Path(solo["root"]) / "living-water" / "tenant.toml").read_text()
    accounts = tomllib.loads(text)["expenses"]["category_accounts"]
    assert list(accounts) == STARTER


# ---- the card -----------------------------------------------------------------------


def _snap(solo, said: str, claim: Claim, photo: bytes = JPEG) -> str:  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    desk = ReceiptDesk()
    token = desk.issue("daniel-reyes", "Daniel Reyes", claim, said)
    status, body = take_photo(
        desk,
        token,
        photo,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    assert status == 200, body
    return body["say"]


def _month(solo):  # noqa: F811
    return run(
        "living-water",
        "expenses",
        "month",
        params={"month": "2026-10"},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _cards(solo) -> list[dict]:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    rows = con.execute(
        "SELECT status, params_json FROM approval_queue WHERE action_type = ?", (REVIEW_CARD,)
    ).fetchall()
    con.close()
    cards = [{"status": s, **json.loads(p)} for s, p in rows]
    return [c for c in cards if c.get("month") == "2026-10"]


def _door(solo, person: str) -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        receipts=Receipts(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person=person,
            desk=ReceiptDesk(),
            base_url="https://tim.example.org/receipt",
        ),
        today="2026-10-13",
    )


def test_the_card_reads_day_amount_and_category(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    said = _snap(solo, "4,300 rupees for the bus to Pokhara", Claim(Decimal("4300"), "", "rupees"))
    assert "under Travel" in said
    result = _month(solo)
    assert result.status != "error", result.summary
    [card] = _cards(solo)
    assert card["lines"] == (
        "2026-10-12: $28.06 (NPR 4,300.00 at 153.26 per dollar on 2026-10-12), Travel"
    )
    assert "?" not in card["lines"] and "_receipt_" not in card["lines"]


def test_words_that_name_no_category_read_uncategorized(solo):  # noqa: F811
    _snap(solo, "here's a $9 receipt", Claim(Decimal("9"), "USD", "$"))
    _month(solo)
    [card] = _cards(solo)
    assert card["lines"] == "2026-10-12: $9.00, Uncategorized"


# ---- the reviewer places it ---------------------------------------------------------


def _line(tools: catalog.Tools) -> dict:
    [month] = tools.call("month_report", {"month": "2026-10"})["people"]
    return month["lines"][0]


def test_the_reviewer_moves_a_receipt_by_asking_and_the_card_shows_it(solo):  # noqa: F811
    _snap(solo, "here's a $9 receipt", Claim(Decimal("9"), "USD", "$"))
    _month(solo)
    linda = _door(solo, "linda-park")
    line = _line(linda)
    assert line["category"] == "Uncategorized" and line["fund"] is None  # one fund

    put = linda.call(
        "set_category",
        {"line": line["source"], "category": "meals", "said": "that $9 one was lunch"},
    )
    assert put["status"] == "placed" and "under Meals" in put["say"]
    assert _line(linda)["category"] == "Meals"
    cards = _cards(solo)
    assert [c["status"] for c in cards] == ["superseded", "pending"]
    assert cards[-1]["lines"].endswith("Meals")


def test_the_person_moves_their_own_receipt(solo):  # noqa: F811
    _snap(solo, "here's a $9 receipt", Claim(Decimal("9"), "USD", "$"))
    daniel = _door(solo, "daniel-reyes")
    line = _line(daniel)
    put = daniel.call(
        "set_category", {"line": line["source"], "category": "Travel", "said": "it was a taxi"}
    )
    assert put["status"] == "placed"
    assert _line(daniel)["category"] == "Travel"


def test_a_category_the_ministry_lacks_gets_its_list_not_a_guess(solo):  # noqa: F811
    _snap(solo, "here's a $9 receipt", Claim(Decimal("9"), "USD", "$"))
    linda = _door(solo, "linda-park")
    put = linda.call(
        "set_category", {"line": _line(linda)["source"], "category": "Software", "said": "x"}
    )
    assert put["status"] == "needs_category" and "Ministry Supplies" in put["say"]
    assert _line(linda)["category"] == "Uncategorized"


def test_a_line_that_is_not_a_waiting_receipt_cannot_move(solo):  # noqa: F811
    put = _door(solo, "linda-park").call(
        "set_category", {"line": "expense_line#1", "category": "Travel", "said": "x"}
    )
    assert put["status"] == "not_found"


def test_both_the_filer_and_the_reviewer_are_offered_it(solo):  # noqa: F811
    for who in ("daniel-reyes", "linda-park"):
        assert "set_category" in {s.name for s in _door(solo, who).specs()}, who


# ---- one fund: never named on every line (the 10/14 practice run) -------------------


def test_a_fund_is_named_only_when_the_lines_span_more_than_one():
    from core.agents.expenses.jobs import _line_words

    line = {
        "file": "2026-10-12_receipt_x.jpg",
        "expense_date": "2026-10-12",
        "amount_cents": 900,
        "category": "Meals",
        "project": "General",
    }
    assert _line_words(line, "fund", funds=1) == "2026-10-12: $9.00, Meals"
    assert _line_words(line, "fund", funds=2) == "2026-10-12: $9.00, Meals, General fund"
