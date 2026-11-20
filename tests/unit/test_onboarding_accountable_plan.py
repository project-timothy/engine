"""Setup writes the ministry's accountable plan (chunk 3, the owner's go of
2026-10-16).

A solo ministry with a reviewer, or a church with a board, starts with a
plan in its rules folder: the words a board adopts, and the plan's numbers
as rules the cash clock reads once someone it does not cover says yes. The
ceiling on cash comes from setup's answer; the days are the safe harbor's.
A ministry with nobody to adopt it, a business, and a sending agency (which
keeps its own handbook) get none.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal

import pytest

from core.engine.authority_gate import load_policy
from core.onboarding import apply, next_question, plan, record
from core.policy import find_quote


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return root


def _onboard(root, slug: str, values: dict):
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return apply(plan(answers, slug), root=root, run_audit=False).tenant_dir


SOLO = {
    "who": "nonprofit-solo",
    "legal_name": "Living Water Mozambique, Inc.",
    "your_name": "Daniel Reyes",
    "reviewer": "Linda Park, linda@example.org",
    "cash_on_hand": "2,000",
}


def test_a_solo_ministry_with_a_reviewer_gets_a_plan_she_adopts(world):
    directory = _onboard(world, "living-water", SOLO)
    data = tomllib.loads((directory / "rules" / "accountable-plan.toml").read_text())
    assert data["plan"] is True
    assert data["adopted_by"] == "reviewer"
    assert data["document"] == "Living Water Mozambique, Inc. accountable plan"
    assert {tuple(r["who"]) for r in data["rule"]} == {("missionary",)}

    authority = load_policy("living-water", tenants_root=world)
    assert authority.rules_error == ""
    [layer] = authority.rules.layers
    held = {r.id: r for r in layer.rules}
    assert held["cash-on-hand"].cash_up_to == Decimal("2000.00")
    assert held["account-within"].account_within == 60
    assert held["return-within"].return_within == 120
    assert held["receipt-from"].over == Decimal("74.99") and held["receipt-from"].needs.receipt


def test_every_rule_quotes_the_plan_words_word_for_word(world):
    directory = _onboard(world, "living-water", SOLO)
    words = (directory / "rules" / "accountable-plan.txt").read_text(encoding="utf-8")
    assert 'Living Water Mozambique, Inc. (the "ministry")' in words
    assert "Cash on hand is not to exceed the equivalent of $2,000" in words
    assert "${" not in words
    rules = load_policy("living-water", tenants_root=world).rules
    for rule in rules.rules:
        assert find_quote(rule.source.quote, [words]) is not None, rule.id


def test_the_plan_names_no_one_but_the_ministry(world):
    directory = _onboard(world, "living-water", SOLO)
    for name in ("accountable-plan.txt", "accountable-plan.toml"):
        text = (directory / "rules" / name).read_text(encoding="utf-8")
        assert "Daniel" not in text and "Linda" not in text


def test_a_solo_ministry_with_no_reviewer_gets_no_plan_yet(world):
    directory = _onboard(world, "living-water", {**SOLO, "reviewer": ""})
    assert not (directory / "rules" / "accountable-plan.toml").exists()


def test_a_church_plan_covers_who_files_and_its_board_adopts_it(world):
    directory = _onboard(
        world,
        "grace",
        {
            "who": "nonprofit-small",
            "legal_name": "Grace Fellowship",
            "your_name": "Carol Jennings",
            "your_role": "treasurer",
            "people": "Mark Ellison = pastor\nRuth Hollis = missionary\nDon Pruitt = board",
        },
    )
    data = tomllib.loads((directory / "rules" / "accountable-plan.toml").read_text())
    assert data["adopted_by"] == "board"
    assert set(data["rule"][0]["who"]) == {"treasurer", "missionary"}
    assert data["rule"][0]["cash_up_to"] == "1500.00"


def test_a_business_gets_no_plan(world):
    directory = _onboard(
        world,
        "acme",
        {
            "who": "commercial-solo",
            "legal_name": "Acme Widgets LLC",
            "your_name": "Pat Doe",
            "reviewer": "Sam Roe, sam@example.org",
        },
    )
    assert not (directory / "rules" / "accountable-plan.toml").exists()


# ---- the roles the ministry holds (#583, 2026-10-27) --------------------------------
# A ministry whose roles aren't the shape's defaults (here a director who reviews
# and a field worker who files) got a plan naming "reviewer" and "missionary",
# roles nobody there holds, so its rules never loaded. The plan names the
# ministry's own roles.


def _renamed_solo_policy():
    from core.authority import parse_policy
    from core.engine.init import render

    text = render("preview", "A", shape="nonprofit-solo", data_root_rel="preview-data")[
        "authority.toml"
    ]
    text = text.replace("missionary", "field-worker").replace("reviewer", "director")
    return parse_policy(tomllib.loads(text))


def _solo_plan(people):
    from core.onboarding import Plan

    return Plan(
        slug="abel",
        shape="nonprofit-solo",
        archetype="D",
        legal_name="Abel Moore Ministries",
        timezone="UTC",
        fiscal_start=1,
        entity="",
        people=people,
        self_approval_limit=None,
        two_approvals_above=None,
        yes_by_word_up_to=None,
        banned=(),
        spelling="American",
        cash_on_hand="2000",
    )


def test_the_plan_names_the_roles_the_ministry_holds(tmp_path):
    from core.onboarding import _accountable_plan
    from core.policy import load_rulebook

    policy = _renamed_solo_policy()
    people = (("abel-moore", "Abel Moore", "field-worker"), ("ruth-cole", "Ruth Cole", "director"))
    _accountable_plan(tmp_path, _solo_plan(people), policy)
    data = tomllib.loads((tmp_path / "rules" / "accountable-plan.toml").read_text())
    assert data["adopted_by"] == "director"
    assert {tuple(r["who"]) for r in data["rule"]} == {("field-worker",)}
    # and the rules load against the ministry's own roles, so the card can go up
    book = load_rulebook(tmp_path, roles=set(policy.roles), people=None, kinds=None)
    assert [layer.adopted_by for layer in book.layers] == ["director"]


def test_with_nobody_here_who_can_adopt_it_there_is_no_plan_yet(tmp_path):
    from core.onboarding import _accountable_plan

    people = (("abel-moore", "Abel Moore", "field-worker"),)
    _accountable_plan(tmp_path, _solo_plan(people), _renamed_solo_policy())
    assert not (tmp_path / "rules" / "accountable-plan.toml").exists()
