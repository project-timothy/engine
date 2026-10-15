"""Who looks after whom, asked at onboarding (the sending-agency scenario).

Onboarding gives each person their own unit, so a regional director whose
role approves `@own` covered nobody: her yes reached no missionary's report.
An organization is now asked who looks after whom, in plain words ("Ann Lee
= Sam Ortiz, Mei Tan"), and the answer widens the director's units to the
people she looks after, and no one else's. Someone who files their own
reports cannot be made to look after others': that would let one missionary
file and see a neighbor's.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.agents.expenses.jobs import REVIEW_CARD
from core.authority import Request, evaluate, parse_policy
from core.engine.authority_gate import decide_card
from core.engine.kit import load_kit
from core.onboarding import OnboardingError, apply, next_question, plan, record

AGENCY = {
    "who": "nonprofit-organization",
    "legal_name": "Harvest Fields Mission, Inc.",
    "your_name": "Lydia Brooks",
    "your_role": "finance",
    "people": (
        "Ann Lee = regional director\nJoel Mensah = regional director\n"
        "Sam Ortiz = missionary\nMei Tan = missionary\nPriya Das = missionary"
    ),
    "looks_after": "Ann Lee = Sam Ortiz, Mei Tan\nJoel Mensah = Priya Das",
}


def _answers(values: dict) -> dict:
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(tmp_path / "tenants"))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return tmp_path / "tenants"


def test_an_organization_is_asked_who_looks_after_whom_right_after_its_people():
    asked = []
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        asked.append(q["id"])
        answers = record(answers, q["id"], AGENCY.get(q["id"], q.get("default", "")))
    assert asked.index("looks_after") == asked.index("people") + 1
    q = next_question({k: v for k, v in answers.items() if k != "looks_after"})
    assert q is not None and q["id"] == "looks_after"
    assert q["optional"] is True
    for word in ("role", "scope", "permission", "unit"):
        assert word not in (q["ask"] + q.get("help", "")).lower()


@pytest.mark.parametrize(
    ("shape", "role"),
    [("nonprofit-small", "treasurer"), ("nonprofit-solo", ""), ("commercial-small", "owner")],
)
def test_a_church_or_a_small_shop_is_not_asked(shape, role):
    answers = _answers({**AGENCY, "who": shape, "your_role": role, "people": ""})
    assert "looks_after" not in answers


def test_the_answer_reads_name_equals_the_people_they_look_after():
    answers = _answers(AGENCY)
    assert answers["looks_after"] == [
        {"name": "Ann Lee", "looks_after": ["Sam Ortiz", "Mei Tan"]},
        {"name": "Joel Mensah", "looks_after": ["Priya Das"]},
    ]


def test_a_name_nobody_mentioned_is_asked_again():
    base = _answers({**AGENCY, "looks_after": ""})
    with pytest.raises(OnboardingError, match="Carl Webb"):
        record(base, "looks_after", "Ann Lee = Sam Ortiz, Carl Webb")
    with pytest.raises(OnboardingError, match="Nora Quist"):
        record(base, "looks_after", "Nora Quist = Sam Ortiz")


def test_someone_who_files_their_own_cannot_look_after_a_neighbor():
    base = _answers({**AGENCY, "looks_after": ""})
    with pytest.raises(OnboardingError, match="Sam Ortiz"):
        record(base, "looks_after", "Sam Ortiz = Mei Tan")


def test_a_director_approves_the_people_she_looks_after_and_no_one_else(root):
    result = apply(plan(_answers(AGENCY), "harvest"), root=root, run_audit=False)
    policy = parse_policy(load_kit(result.tenant_dir).authority)
    assert set(policy.people["ann-lee"].scopes) == {"ann-lee", "sam-ortiz", "mei-tan"}
    assert set(policy.people["mei-tan"].scopes) == {"mei-tan"}

    def may(who: str, action: str, unit: str) -> bool:
        req = Request(
            who, action, "expense.report", scope=unit, submitter=unit, amount=Decimal("420")
        )
        return evaluate(policy, req).allowed

    assert may("ann-lee", "approve", "mei-tan")
    assert may("ann-lee", "view", "sam-ortiz")
    assert not may("ann-lee", "approve", "priya-das")
    assert not may("ann-lee", "view", "priya-das")
    assert may("joel-mensah", "approve", "priya-das")
    assert not may("joel-mensah", "view", "mei-tan")
    assert not may("mei-tan", "view", "sam-ortiz")


def test_with_no_answer_everyone_keeps_their_own_unit(root):
    result = apply(
        plan(_answers({**AGENCY, "looks_after": ""}), "harvest"), root=root, run_audit=False
    )
    policy = parse_policy(load_kit(result.tenant_dir).authority)
    assert set(policy.people["ann-lee"].scopes) == {"ann-lee"}


def _card(person: str) -> dict:
    return {
        "id": 7,
        "agent": "expenses",
        "action_type": REVIEW_CARD,
        "params": {"person": person, "total_cents": "42000"},
        "created_at": "2026-10-12T12:00:00+00:00",
    }


def test_a_director_decides_the_card_of_someone_she_looks_after(root):
    # The card names the person it is about; deciding it is evaluated in
    # that person's unit, as seeing it is. Before, deciding a card named no
    # unit, so no one whose yes is limited to the people they look after
    # could decide any card at all.
    result = apply(plan(_answers(AGENCY), "harvest"), root=root, run_audit=False)
    policy = parse_policy(load_kit(result.tenant_dir).authority)

    def decide(who: str, person: str):
        return decide_card(
            policy,
            who,
            _card(person),
            decision="approved",
            at_terminal=True,
            delegations=[],
            today=date(2026, 10, 12),
            now="2026-10-12T13:00:00+00:00",
        )

    mine = decide("ann-lee", "Mei Tan")
    assert mine.refusal == "" and mine.final
    assert decide("ann-lee", "Priya Das").refusal
    assert decide("mei-tan", "Mei Tan").refusal  # nobody approves their own
