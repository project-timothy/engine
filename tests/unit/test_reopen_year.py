"""A closed year can be reopened, by the right people for each size of
ministry (the owner's decision, 2026-11-10):

- solo or small: the missionary reopens it alone, after Tim warns it may
  change last year's books (the 1099s and the 990 came from them);
- a church: the bookkeeper asks and a supervisor says yes, two people;
- an organization: accounting asks and an accounting supervisor says yes.

The card names who asked, so each ministry's own safeguards decide who may
say yes: a solo ministry lets a person approve their own, a church and an
organization never do. The latest yes stands: a reopen opens the year again,
and a close after it closes it again."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from core.agents.expenses import year_close as yc
from core.agents.expenses.jobs import CARD_AUTHORITY
from core.authority import card_request, evaluate, parse_policy
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_year_close import _closed_2026, _db, _yes

TEMPLATES = Path(__file__).resolve().parents[2] / "tenants" / "_templates" / "authority"
RULE = CARD_AUTHORITY[yc.REOPEN_CARD]


def _cards(solo, kind: str) -> list[dict]:  # noqa: F811
    con = sqlite3.connect(_db(solo))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ? ORDER BY id",
        (kind,),
    ).fetchall()
    con.close()
    return [{"id": r["id"], "status": r["status"], **json.loads(r["params_json"])} for r in rows]


# ---- by code -----------------------------------------------------------------------


def _card(kind: str, status: str, year: str, at: str | None) -> dict:
    return {"action_type": kind, "status": status, "params": {"year": year}, "resolved_at": at}


def test_the_latest_yes_stands_a_reopen_opens_and_a_close_closes_again():
    close, reopen = yc.CLOSE_CARD, yc.REOPEN_CARD
    cards = [
        _card(close, "approved", "2026", "2027-01-20T15:00:00Z"),
        _card(reopen, "approved", "2026", "2027-02-03T15:00:00Z"),
    ]
    assert yc.closed_years(cards) == {}
    cards.append(_card(close, "approved", "2026", "2027-03-01T15:00:00Z"))
    assert yc.closed_years(cards)["2026"].on == "2027-03-01"


def test_a_reopen_turned_down_or_waiting_leaves_the_year_closed():
    cards = [
        _card(yc.CLOSE_CARD, "approved", "2026", "2027-01-20T15:00:00Z"),
        _card(yc.REOPEN_CARD, "rejected", "2026", "2027-02-03T15:00:00Z"),
        _card(yc.REOPEN_CARD, "pending", "2026", None),
    ]
    assert yc.closed_years(cards)["2026"].on == "2027-01-20"


def test_a_card_without_its_kind_reads_as_a_close():
    cards = [{"status": "approved", "params": {"year": "2025"}, "resolved_at": "2026-01-02"}]
    assert list(yc.closed_years(cards)) == ["2025"]


# ---- who may say yes, by the ministry's own shape ----------------------------------


def _policy(shape: str, people: dict) -> object:
    data = tomllib.loads((TEMPLATES / f"nonprofit-{shape}.toml.tmpl").read_text())
    data["people"] = people
    return parse_policy(data)


def _decides(policy, who: str, asked_by: str) -> bool:
    params = {"year": "2026", "by": asked_by}
    return evaluate(policy, card_request(RULE, who, params, policy)).allowed


def test_solo_the_missionary_reopens_alone():
    policy = _policy(
        "solo",
        {
            "caleb": {"roles": ["missionary"], "name": "Caleb Meer"},
            "dad": {"roles": ["reviewer"], "name": "Paul Weston"},
        },
    )
    assert _decides(policy, "caleb", "caleb")
    assert not _decides(policy, "dad", "dad")  # a reviewer looks over, never decides
    assert not _decides(policy, "dad", "caleb")


def test_a_church_the_bookkeeper_asks_and_a_supervisor_says_yes():
    policy = _policy(
        "small",
        {
            "carol": {"roles": ["treasurer"], "name": "Carol Jennings"},
            "mark": {"roles": ["pastor"], "name": "Mark Ellison"},
            "don": {"roles": ["board"], "name": "Don Pruitt"},
        },
    )
    assert not _decides(policy, "carol", "carol")  # never the one who asked
    assert _decides(policy, "mark", "carol")
    assert not _decides(policy, "don", "carol")  # the board looks over, never decides


def test_an_organization_accounting_asks_and_its_supervisor_says_yes():
    policy = _policy(
        "organization",
        {
            "ana": {"roles": ["finance"], "name": "Ana Ruiz"},
            "lee": {"roles": ["finance"], "name": "Lee Park"},
            "rd": {"roles": ["regional-director"], "scopes": ["asia"], "name": "Ray Dunn"},
        },
    )
    assert not _decides(policy, "ana", "ana")
    assert _decides(policy, "lee", "ana")
    assert not _decides(policy, "rd", "ana")  # a region's director reaches only their own


# ---- the door ----------------------------------------------------------------------


def test_the_missionary_reopens_alone_after_the_warning(solo):  # noqa: F811
    daniel, _linda = _closed_2026(solo)
    asked = daniel.call("reopen_year", {"year": "2026", "said": "reopen 2026"})
    assert asked["status"] == "waiting", asked
    [card] = _cards(solo, yc.REOPEN_CARD)
    assert card["year"] == "2026" and card["by"] == "daniel-reyes"
    for said in (asked["say"], card["warning"]):
        assert "1099" in said and "990" in said, said
    assert asked["say"].index("1099") < asked["say"].index("decide_card")  # warned first
    assert str(card["id"]) in asked["say"]


def test_a_reviewer_who_asks_waits_on_the_missionarys_yes(solo):  # noqa: F811
    _daniel, linda = _closed_2026(solo)
    asked = linda.call("reopen_year", {"year": "2026", "said": "reopen 2026"})
    assert asked["status"] == "waiting", asked
    assert "decide_card" not in asked["say"] and "someone else" in asked["say"], asked
    assert "1099" in asked["say"]


def test_asking_to_reopen_twice_is_the_same_card(solo):  # noqa: F811
    _daniel, linda = _closed_2026(solo)
    linda.call("reopen_year", {"year": "2026", "said": "reopen 2026"})
    again = linda.call("reopen_year", {"year": "2026", "said": "reopen it please"})
    assert again["status"] == "waiting"
    assert len(_cards(solo, yc.REOPEN_CARD)) == 1


def test_an_open_year_has_nothing_to_reopen(solo):  # noqa: F811
    _daniel, linda = _closed_2026(solo)
    said = linda.call("reopen_year", {"year": "2025", "said": "reopen 2025"})
    assert said["status"] == "open" and "2025" in said["say"], said
    assert _cards(solo, yc.REOPEN_CARD) == []


def test_after_the_reopen_the_year_takes_changes_and_closes_again(solo):  # noqa: F811
    daniel, linda = _closed_2026(solo)
    linda.call("reopen_year", {"year": "2026", "said": "reopen 2026"})
    [card] = _cards(solo, yc.REOPEN_CARD)
    _yes(solo, card["id"], at="2027-02-03T15:00:00+00:00")
    said = daniel.call("send_month_for_review", {"month": "2026-12"})
    assert said["status"] != "year_closed", said
    again = linda.call("close_year", {"year": "2026", "said": "close 2026 again"})
    assert again["status"] == "waiting", again
    closes = _cards(solo, yc.CLOSE_CARD)
    assert [c["status"] for c in closes] == ["approved", "pending"]


@pytest.mark.parametrize("year", ["26", "next year"])
def test_a_reopen_asks_for_four_digits(solo, year):  # noqa: F811
    _daniel, linda = _closed_2026(solo)
    said = linda.call("reopen_year", {"year": year, "said": "reopen"})
    assert said["status"] == "needs", said
