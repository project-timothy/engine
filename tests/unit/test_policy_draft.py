"""Drafting rules from a new handbook copy (board item t-rule-drafting, the
owner's go of 2026-10-29).

The morning watch already cards a new copy of a ministry's handbook. Now the
person who decides the ministry's rules asks their own AI to draft the new
ones: Tim hands it the pages, and each rule it drafts comes back through
``draft_rule``. Code holds the line, not the AI: the quote must be in the new
copy word for word, the rule must read as one, and a lower layer may only be
stricter. A draft is a rule like any other, carded on its own, and the rule it
replaces keeps counting until the draft gets its yes. No model runs on the box.
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest

from core.agents.policy.jobs import RULE_CARD, rules_in_force
from core.engine.authority_gate import load_policy
from core.ledger import Ledger
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_policy_ask_first import OK_FIRST
from tests.unit.test_policy_month import _approve_rules
from tests.unit.test_policy_watch import POLICY, _copy

NEW = POLICY.replace("up to $40 each", "up to $50 each") + (
    "\fReceipts\nMinistry supplies over $500 are bought with a receipt in hand.\n"
)
MEALS_50 = {
    "document": "Living Water spending policy",
    "replaces": "meals-cap",
    "says": "A meal is up to $50.",
    "kinds": ["Meals"],
    "item_up_to": "50",
    "section": "Meals",
    "page": 1,
    "quote": "Meals are reimbursed up to $50 each.",
}


def _changed(solo) -> None:  # noqa: F811
    """Rules drafted from the old copy and approved; then the handbook changed."""
    _copy(solo, NEW.encode(), pin=POLICY.encode())
    _approve_rules(solo)


def _file(solo) -> str:  # noqa: F811
    return (Path(solo["root"]) / "living-water" / "rules" / "policy.toml").read_text()


def _in_force(solo) -> list[str]:  # noqa: F811
    authority = load_policy("living-water", tenants_root=solo["root"])
    with Ledger.open(solo["ledger_root"]) as ledger:
        return [r.id for r in rules_in_force(ledger, "living-water", authority.rules)]


def _pending(solo) -> list[dict]:  # noqa: F811
    import json

    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    rows = con.execute(
        "SELECT id, params_json FROM approval_queue WHERE action_type = ? AND status = 'pending'",
        (RULE_CARD,),
    ).fetchall()
    con.close()
    return [{"id": i, **json.loads(p)} for i, p in rows]


def _approve(solo, card: int) -> None:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute("UPDATE approval_queue SET status = 'approved' WHERE id = ?", (card,))
    con.commit()
    con.close()


# ---- who is offered it ----------------------------------------------------------


def test_drafting_is_offered_to_who_decides_the_rules_where_a_handbook_is_watched(solo):  # noqa: F811
    _changed(solo)
    names = {s.name for s in _door(solo, "daniel-reyes").specs()}
    assert {"handbook_changes", "draft_rule"} <= names
    # the reviewer looks over spending but doesn't decide the ministry's rules
    names = {s.name for s in _door(solo, "linda-park").specs()}
    assert "draft_rule" not in names and "handbook_changes" not in names


def test_a_ministry_that_watches_no_handbook_is_not_offered_drafting(solo):  # noqa: F811
    from tests.unit.test_policy_month import _rules

    _rules(solo)  # rules with no [copy]
    names = {s.name for s in _door(solo, "daniel-reyes").specs()}
    assert "draft_rule" not in names


# ---- what changed --------------------------------------------------------------


def test_handbook_changes_names_the_rule_whose_words_are_gone_and_hands_over_the_pages(solo):  # noqa: F811
    _changed(solo)
    said = _door(solo, "daniel-reyes").call("handbook_changes", {})
    [doc] = [d for d in said["documents"] if d["document"] == "Living Water spending policy"]
    assert [g["rule"] for g in doc["gone"]] == ["meals-cap"]
    assert doc["gone"][0]["quote"] == "Meals are reimbursed up to $40 each."
    text = " ".join(p["text"] for p in doc["pages"])
    assert "Meals are reimbursed up to $50 each." in text
    assert "bought with a receipt in hand" in text
    one = _door(solo, "daniel-reyes").call(
        "handbook_changes", {"document": "Living Water spending policy", "page": 3}
    )
    assert [p["page"] for p in one["documents"][0]["pages"]] == [3]


# ---- a draft ------------------------------------------------------------------


@pytest.mark.security("SEC-20")
def test_a_draft_quoting_the_new_copy_is_carded_and_the_old_rule_counts_until_its_yes(solo):  # noqa: F811
    _changed(solo)
    said = _door(solo, "daniel-reyes").call("draft_rule", MEALS_50)
    assert said["status"] == "drafted", said
    assert "A meal is up to $50." in said["say"]
    held = tomllib.loads(_file(solo))
    [draft] = [r for r in held["rule"] if r.get("replaces") == "meals-cap"]
    assert draft["item_up_to"] == "50" and draft["source"]["page"] == 1
    [card] = [c for c in _pending(solo) if c.get("says") == "A meal is up to $50."]
    assert card["what"].endswith('in place of "A meal is up to $40."')
    assert "meals-cap" in _in_force(solo) and draft["id"] not in _in_force(solo)
    _approve(solo, card["id"])
    assert draft["id"] in _in_force(solo) and "meals-cap" not in _in_force(solo)


def test_a_new_rule_with_nothing_to_replace_is_drafted_too(solo):  # noqa: F811
    _changed(solo)
    said = _door(solo, "daniel-reyes").call(
        "draft_rule",
        {
            "document": "Living Water spending policy",
            "says": "Supplies over $500 need a receipt.",
            "kinds": ["Ministry Supplies"],
            "over": "500",
            "needs": ["receipt"],
            "section": "Receipts",
            "page": 3,
            "quote": "Ministry supplies over $500 are bought with a receipt in hand.",
        },
    )
    assert said["status"] == "drafted", said
    assert any(c.get("says") == "Supplies over $500 need a receipt." for c in _pending(solo))


@pytest.mark.security("SEC-20")
def test_a_quote_not_word_for_word_in_the_copy_is_refused_and_nothing_is_written(solo):  # noqa: F811
    _changed(solo)
    before = _file(solo)
    said = _door(solo, "daniel-reyes").call(
        "draft_rule", MEALS_50 | {"quote": "Meals are reimbursed up to $60 each."}
    )
    assert said["status"] == "refused"
    assert "word for word" in said["say"]
    assert _file(solo) == before


def test_a_draft_that_does_not_read_as_a_rule_is_refused_with_why(solo):  # noqa: F811
    _changed(solo)
    before = _file(solo)
    bad = {k: v for k, v in MEALS_50.items() if k != "item_up_to"}  # caps nothing, needs nothing
    said = _door(solo, "daniel-reyes").call("draft_rule", bad)
    assert said["status"] == "refused"
    assert "caps an amount or needs something" in said["say"]
    replaces_nothing = _door(solo, "daniel-reyes").call(
        "draft_rule", MEALS_50 | {"replaces": "no-such-rule"}
    )
    assert replaces_nothing["status"] == "refused" and "no-such-rule" in replaces_nothing["say"]
    assert _file(solo) == before


def test_a_plan_is_never_drafted_this_way(solo):  # noqa: F811
    _changed(solo)
    said = _door(solo, "daniel-reyes").call(
        "draft_rule", MEALS_50 | {"document": "Living Water Mozambique, Inc. accountable plan"}
    )
    assert said["status"] == "refused"
    assert "plan" in said["say"]


def test_drafting_twice_is_one_draft(solo):  # noqa: F811
    _changed(solo)
    _door(solo, "daniel-reyes").call("draft_rule", MEALS_50)
    again = _door(solo, "daniel-reyes").call("draft_rule", MEALS_50)
    assert again["status"] == "drafted"
    held = tomllib.loads(_file(solo))
    assert len([r for r in held["rule"] if r.get("replaces") == "meals-cap"]) == 1


# ---- core/policy: replaces ----------------------------------------------------


def test_naming_nothing_to_replace_never_changes_a_rules_fingerprint():
    # the sha a yes was given to before drafts existed
    assert OK_FIRST.sha == "44e356988d47f750789949ec5d12e7b0c565ace14c2f396a78036e4a21895320"
