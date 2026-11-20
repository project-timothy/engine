"""Agency packs (core/pack, chunk 8): a sending agency's roles, numbers and
handbook rules in one folder, every number cited, set up as its own ministry
(the owner's decisions of 2026-10-15 and 2026-10-16)."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from core.pack import PackError, check, load_pack

HARVEST = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "harvest"


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    copy = tmp_path / "harvest"
    shutil.copytree(HARVEST, copy)
    return copy


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_the_example_pack_loads_and_checks_clean():
    pack = load_pack(HARVEST)
    assert pack.name == "Harvest Fields Mission"
    assert pack.shape == "nonprofit-organization"
    assert {"team-leader", "regional-director", "finance", "compliance"} <= set(pack.roles)
    assert "agent-intake" not in pack.roles  # a person never holds an agent's role
    assert [n.value for n in pack.numbers] == [Decimal("4000"), Decimal("10000")]
    assert pack.numbers[0].source.page == 3
    assert check(pack) == []


def test_an_amount_nobody_cited_is_named(pack_dir):
    _edit(
        pack_dir / "authority.toml",
        "approve:purchase.order@own<=4000",
        "approve:purchase.order@own<=4500",
    )
    problems = check(load_pack(pack_dir))
    assert any("roles.team-leader" in p and "4500" in p and "cites" in p for p in problems)
    assert any("roles.team-leader" in p and "4000" in p for p in problems)  # the stale one


def test_a_safeguard_amount_needs_its_words_too(pack_dir):
    _edit(pack_dir / "authority.toml", "yes_by_word_up_to = 0", "yes_by_word_up_to = 750")
    problems = check(load_pack(pack_dir))
    assert any("safeguards.yes_by_word_up_to" in p and "750" in p for p in problems)


def test_a_number_with_no_words_does_not_load(pack_dir):
    _edit(pack_dir / "pack.toml", 'quote = "your regional director approves up to $10,000"\n', "")
    with pytest.raises(PackError, match="quote"):
        load_pack(pack_dir)


def test_a_number_whose_words_left_the_copy_is_named(pack_dir):
    _edit(pack_dir / "pack.toml", "approves up to $10,000", "approves up to $12,000")
    problems = check(load_pack(pack_dir))
    assert any("roles.regional-director" in p and "not in" in p for p in problems)


def test_a_rule_naming_a_role_the_pack_lacks_is_named(pack_dir):
    _edit(
        pack_dir / "rules" / "harvest-handbook.toml",
        'approver = "team-leader"',
        'approver = "area-director"',
    )
    problems = check(load_pack(pack_dir))
    assert any("area-director" in p for p in problems)


def test_a_rule_whose_quote_left_the_copy_is_named(pack_dir):
    _edit(pack_dir / "rules" / "harvest-handbook.txt", "your team leader approves", "you approve")
    problems = check(load_pack(pack_dir))
    assert any("equipment-ok-first" in p and "not in" in p for p in problems)


def test_a_pack_names_no_people(pack_dir):
    _edit(pack_dir / "authority.toml", "[people]\n", '[people]\nann = { roles = ["finance"] }\n')
    with pytest.raises(PackError, match="people"):
        load_pack(pack_dir)


def test_a_pack_sets_up_an_organization_only(pack_dir):
    _edit(pack_dir / "pack.toml", 'shape = "nonprofit-organization"', 'shape = "nonprofit-small"')
    with pytest.raises(PackError, match="organization"):
        load_pack(pack_dir)


def test_a_folder_with_no_pack_says_so(tmp_path):
    with pytest.raises(PackError, match="pack.toml"):
        load_pack(tmp_path)


def test_a_route_amount_needs_its_words(pack_dir):
    _edit(
        pack_dir / "authority.toml",
        "backup_after_days = 5\n",
        'backup_after_days = 5\n\n[[routing.route]]\nresource = "payment"\nabove = 5000\n'
        'steps = [{ role = "finance" }, { role = "regional-director" }]\n',
    )
    problems = check(load_pack(pack_dir))
    assert any("routing.payment" in p and "5000" in p for p in problems)


# ---- a ministry set up from a pack ------------------------------------------------

from core.agents.policy.jobs import ASK_CARD, CARD_AUTHORITY  # noqa: E402
from core.authority import (  # noqa: E402
    card_request,  # noqa: E402
    evaluate,
    parse_policy,
)
from core.engine.cli import main as engine_main  # noqa: E402
from core.engine.config import load_tenant  # noqa: E402
from core.engine.kit import load_kit  # noqa: E402
from core.onboarding import OnboardingError, apply, next_question, plan, record  # noqa: E402
from core.onboarding.mcp import OnboardingTools  # noqa: E402

HARVEST_PEOPLE = {
    "who": "Our ministry organization",
    "legal_name": "Harvest Fields Mission, Inc.",
    "your_name": "Lydia Brooks",
    "your_role": "finance",
    "people": "Ann Lee = team-leader\nJoel Mensah = team-leader\nRita Cole = regional-director\n"
    "Sam Ortiz = missionary\nMei Tan = missionary\nPriya Das = missionary\n"
    "Ken Moss = compliance",
    "looks_after": "Ann Lee = Sam Ortiz, Mei Tan\nJoel Mensah = Priya Das\n"
    "Rita Cole = Sam Ortiz, Mei Tan, Priya Das",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return tmp_path, root


def _from_pack(values: dict) -> tuple[dict, list[str]]:
    answers: dict = {"pack": str(HARVEST)}
    asked = []
    while (q := next_question(answers)) is not None:
        asked.append(q["id"])
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers, asked


def test_setup_from_a_pack_offers_its_roles_and_never_asks_its_numbers():
    answers: dict = {"pack": str(HARVEST)}
    while (q := next_question(answers)) is not None and q["id"] != "your_role":
        answers = record(answers, q["id"], HARVEST_PEOPLE.get(q["id"], q.get("default", "")))
    assert q is not None
    assert "team-leader" in q["choices"] and "board" not in q["choices"]
    _answers, asked = _from_pack(HARVEST_PEOPLE)
    assert not {"two_approvals_above", "yes_by_word_up_to", "second_person_above"} & set(asked)


def test_a_pack_sets_up_an_organization_never_a_church():
    with pytest.raises(OnboardingError, match="pack"):
        record({"pack": str(HARVEST)}, "who", "Our church")


def test_a_role_the_pack_lacks_is_refused():
    answers = record({"pack": str(HARVEST)}, "who", "Our ministry organization")
    with pytest.raises(OnboardingError):
        record(answers, "people", "Sam Ortiz = board")


def test_a_ministry_set_up_from_a_pack_carries_its_roles_rules_and_numbers(world):
    _tmp, root = world
    answers, _asked = _from_pack(HARVEST_PEOPLE)
    result = apply(plan(answers, "harvest"), root=root, run_audit=False)
    directory = result.tenant_dir
    policy = parse_policy(load_kit(directory).authority)
    assert policy.people["ann-lee"].roles == ("team-leader",)
    assert policy.safeguards.self_approval == "never"
    assert policy.safeguards.yes_by_word_up_to == 0  # the pack's, not setup's default
    assert (directory / "rules" / "harvest-handbook.toml").is_file()
    assert (directory / "rules" / "harvest-handbook.txt").is_file()
    assert (directory / "pack.toml").is_file()
    accounts = load_tenant("harvest", tenants_root=root).expenses.category_accounts
    assert {"Work Equipment", "National Partners", "Travel"} <= set(accounts)
    assert engine_main(["rules", "harvest", "--root", str(root)]) == 0


def test_a_team_leader_okays_only_her_own_peoples_purchases(world):
    _tmp, root = world
    answers, _asked = _from_pack(HARVEST_PEOPLE)
    result = apply(plan(answers, "harvest"), root=root, run_audit=False)
    policy = parse_policy(load_kit(result.tenant_dir).authority)
    rule = CARD_AUTHORITY[ASK_CARD]

    def may(who: str, person: str, amount: str) -> bool:
        params = {"person": person, "amount": amount, "approver_role": "team-leader"}
        return evaluate(policy, card_request(rule, who, params, policy)).allowed

    assert may("ann-lee", "Mei Tan", "1400.00")
    assert not may("joel-mensah", "Mei Tan", "1400.00")  # not one of his
    assert not may("ken-moss", "Mei Tan", "1400.00")  # compliance sees, never okays
    assert not may("mei-tan", "Mei Tan", "1400.00")  # nobody okays their own


def test_the_onboarding_door_sets_up_from_the_hosts_pack_and_never_shows_it(tmp_path):
    tools = OnboardingTools(answers_dir=tmp_path, root=tmp_path / "tenants", pack=HARVEST)
    state = tools.call(
        "onboarding_record", {"slug": "harvest", "id": "who", "value": "Our ministry organization"}
    )
    assert "pack" not in state["answers"]
    assert "two_approvals_above" not in json.dumps(state["next"])


def test_pack_check_says_what_to_fix(pack_dir, capsys):
    assert engine_main(["pack", "check", str(HARVEST)]) == 0
    _edit(pack_dir / "authority.toml", "<=4000", "<=4500")
    assert engine_main(["pack", "check", str(pack_dir)]) == 1
    assert "no number cites" in capsys.readouterr().out
