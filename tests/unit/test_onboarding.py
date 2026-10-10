"""The onboarding conversation (issue #442, docs/tenant-kit-design.md section 6).

An agent runs it; the engine holds the questions and the rules as data.
`engine onboard <slug>` speaks JSON so any agent can drive it: it prints the
next question (plain words, choices, a default for the shape), records an
answer, and on `--apply` renders the tenant with `engine init`, writes the
answers into the kit (people, limits, entity, voice), and runs doctor. The
first question is who this is for; it picks the shape, and the shape picks
which questions follow, so a solo missionary answers a handful.
"""

from __future__ import annotations

import json
import tomllib
from decimal import Decimal

import pytest

from core.authority import Request, evaluate, parse_policy
from core.engine.cli import main as engine_main
from core.engine.config import load_tenant
from core.engine.kit import load_kit
from core.onboarding import (
    OnboardingError,
    apply,
    load_questions,
    missing,
    next_question,
    plan,
    record,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return tmp_path, root


def _answer_all(answers: dict, values: dict) -> dict:
    """Answer every question the conversation asks, in its order, from
    ``values`` or the question's own default."""
    while (q := next_question(answers)) is not None:
        value = values.get(q["id"], q.get("default", ""))
        answers = record(answers, q["id"], value)
    return answers


# ---- the questions ---------------------------------------------------------------


def test_the_first_question_is_who_this_is_for():
    q = next_question({})
    assert q["id"] == "who"
    assert len(q["choices"]) == 6
    assert "role" not in q["ask"].lower()


def test_every_question_is_plain_words_with_no_machinery():
    for q in load_questions():
        text = (q.ask + " " + q.help).lower()
        for word in ("scope", "permission", "toml", "safeguard", "principal"):
            assert word not in text, (q.id, word)


def test_a_choice_takes_its_label_or_its_value():
    by_label = record({}, "who", "Our church")
    by_value = record({}, "who", "nonprofit-small")
    assert by_label == by_value == {"who": "nonprofit-small"}


def test_an_answer_outside_the_choices_refuses():
    with pytest.raises(OnboardingError, match="who"):
        record({}, "who", "a spaceship")


def test_a_solo_missionary_answers_a_handful():
    answers = _answer_all(
        {}, {"who": "nonprofit-solo", "legal_name": "Ellis Mission Inc.", "your_name": "Jo Ellis"}
    )
    assert len(answers) <= 9
    assert "people" not in answers and "two_approvals_above" not in answers


def test_a_church_is_asked_for_its_people_and_its_limits():
    answers = {"who": "nonprofit-small"}
    asked = []
    given = {"legal_name": "Grace", "your_name": "Pat", "your_role": "treasurer", "people": []}
    while (q := next_question(answers)) is not None:
        asked.append(q["id"])
        answers = record(answers, q["id"], given.get(q["id"], q.get("default", "")))
    assert {"people", "second_person_above", "two_approvals_above"} <= set(asked)


def test_the_role_choices_come_from_the_shape_and_hide_the_agents():
    q = next_question({"who": "nonprofit-small", "legal_name": "Grace", "your_name": "Pat"})
    assert q["id"] == "your_role"
    assert "treasurer" in q["choices"] and not any(c.startswith("agent-") for c in q["choices"])


def test_defaults_follow_the_shape():
    church = {"who": "nonprofit-small"}
    company = {"who": "commercial-organization"}
    assert _default(church, "two_approvals_above") == "2500"
    assert _default(company, "two_approvals_above") == "10000"


def _default(answers: dict, qid: str) -> str:
    q = next(q for q in load_questions() if q.id == qid)
    return q.default_for(answers["who"])


@pytest.mark.parametrize("value", ["$1,250.00", "1250", 1250])
def test_an_amount_takes_the_ways_people_write_money(value):
    answers = record({"who": "commercial-small"}, "second_person_above", value)
    assert answers["second_person_above"] == "1250"


def test_people_are_name_equals_role_and_the_role_must_exist():
    base = {"who": "nonprofit-small"}
    answers = record(base, "people", "Sam Lee = board; Pat Kim = missionary")
    assert answers["people"] == [
        {"name": "Sam Lee", "role": "board"},
        {"name": "Pat Kim", "role": "missionary"},
    ]
    with pytest.raises(OnboardingError, match="wizard"):
        record(base, "people", "Sam Lee = wizard")


def test_missing_names_the_required_answers():
    assert "legal_name" in missing({"who": "commercial-small"})


# ---- the plan and the apply --------------------------------------------------------


CHURCH = {
    "who": "nonprofit-small",
    "legal_name": "Grace Chapel",
    "your_name": "Pat Kim",
    "your_role": "treasurer",
    "people": "Sam Lee = board; Jo Ellis = missionary",
    "second_person_above": "750",
    "two_approvals_above": "3000",
    "banned_words": "leverage, synergy",
    "spelling": "en-US",
}


def test_apply_builds_a_tenant_from_the_answers(world):
    _tmp, root = world
    answers = _answer_all({}, CHURCH)
    result = apply(plan(answers, "grace"), root=root, run_audit=False)
    cfg = load_tenant("grace", tenants_root=root)
    assert cfg.identity.legal_name == "Grace Chapel"
    assert cfg.identity.shape == "nonprofit-small"
    assert cfg.books.entity == "church"
    kit = load_kit(result.tenant_dir)
    policy = parse_policy(kit.authority)
    assert set(policy.people) == {"pat-kim", "sam-lee", "jo-ellis"}
    assert policy.people["pat-kim"].roles == ("treasurer",)
    assert policy.safeguards.self_approval_limit == Decimal("750")
    assert policy.safeguards.second_approver_above == Decimal("3000")
    assert {"leverage", "synergy"} <= set(kit.voice["banned"])
    assert result.doctor is not None


def test_the_applied_policy_works_end_to_end(world):
    _tmp, root = world
    result = apply(plan(_answer_all({}, CHURCH), "grace"), root=root, run_audit=False)
    policy = parse_policy(load_kit(result.tenant_dir).authority)
    req = Request(
        "pat-kim", "approve", "expense.report", submitter="jo-ellis", amount=Decimal("100")
    )
    assert evaluate(policy, req).allowed


def test_a_solo_reviewer_gets_the_review_role(world):
    _tmp, root = world
    answers = _answer_all(
        {},
        {
            "who": "nonprofit-solo",
            "legal_name": "Ellis Mission Inc.",
            "your_name": "Jo Ellis",
            "reviewer": "Pastor Dan Reyes",
        },
    )
    result = apply(plan(answers, "ellis"), root=root, run_audit=False)
    policy = parse_policy(load_kit(result.tenant_dir).authority)
    assert policy.people["jo-ellis"].roles == ("missionary",)
    assert policy.people["pastor-dan-reyes"].roles == ("reviewer",)
    assert policy.safeguards.review_after == "monthly"


def test_a_business_entity_lands_in_books(world):
    _tmp, root = world
    answers = _answer_all(
        {},
        {
            "who": "commercial-solo",
            "legal_name": "Acme LLC",
            "your_name": "Lee",
            "entity": "s-corp",
        },
    )
    apply(plan(answers, "acme"), root=root, run_audit=False)
    assert load_tenant("acme", tenants_root=root).books.tax_return == "1120-S"


def test_apply_refuses_while_a_required_answer_is_missing(world):
    _tmp, root = world
    with pytest.raises(OnboardingError, match="legal_name"):
        plan({"who": "commercial-small"}, "acme")
    assert not (root / "acme").exists()


def test_two_people_with_one_name_get_distinct_ids(world):
    _tmp, root = world
    answers = _answer_all({}, {**CHURCH, "people": "Pat Kim = board"})
    result = apply(plan(answers, "grace"), root=root, run_audit=False)
    policy = parse_policy(load_kit(result.tenant_dir).authority)
    assert {"pat-kim", "pat-kim-2"} <= set(policy.people)


# ---- the command, as an agent drives it ----------------------------------------------


def test_the_command_walks_an_agent_through_in_json(world, capsys):
    tmp, root = world
    store = tmp / "answers.json"
    base = ["onboard", "ellis", "--answers", str(store), "--root", str(root)]
    assert engine_main(base) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["next"]["id"] == "who"
    assert engine_main([*base, "--answer", "who=nonprofit-solo"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["next"]["id"] == "legal_name"
    assert json.loads(store.read_text())["who"] == "nonprofit-solo"


def test_the_command_applies_and_reports_doctor(world, capsys):
    tmp, root = world
    store = tmp / "answers.json"
    answers = _answer_all(
        {}, {"who": "nonprofit-solo", "legal_name": "Ellis Mission Inc.", "your_name": "Jo Ellis"}
    )
    store.write_text(json.dumps(answers), encoding="utf-8")
    rc = engine_main(
        ["onboard", "ellis", "--answers", str(store), "--root", str(root), "--apply", "--no-audit"]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["created"].endswith("ellis")
    assert any(line.startswith("kit authority") for line in out["doctor"])
    assert tomllib.loads((root / "ellis" / "tenant.toml").read_text())["identity"]["shape"] == (
        "nonprofit-solo"
    )


def test_the_command_refuses_to_apply_an_unfinished_conversation(world, capsys):
    tmp, root = world
    store = tmp / "answers.json"
    store.write_text(json.dumps({"who": "commercial-small"}), encoding="utf-8")
    rc = engine_main(["onboard", "acme", "--answers", str(store), "--root", str(root), "--apply"])
    assert rc == 2
    assert "legal_name" in capsys.readouterr().err
