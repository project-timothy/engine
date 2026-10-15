"""Each person's own time zone, for the invitation at apply (plan to alpha,
step 5, 2026-10-12).

Carol named "Ruth Hollis = missionary in Nepal". Ruth's welcome link, and
every time Tim tells her later, should read in Kathmandu time, not Ohio's.
The place in a person's line, when it names one zone, is their zone;
everyone else keeps the ministry's. apply writes ``zone`` beside each person
in authority.toml, where the doorkeeper reads it.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from core.authority import parse_policy
from core.onboarding import apply, next_question, plan, record
from core.onboarding.mcp import OnboardingTools

GRACE = {
    "who": "nonprofit-small",
    "legal_name": "Grace Fellowship",
    "your_name": "Carol Jennings",
    "your_role": "treasurer",
    "timezone": "Eastern",
}


def _answers(people: str, **over):
    values = {**GRACE, "people": people, **over}
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers


def test_a_place_in_the_line_is_that_persons_zone():
    p = plan(_answers("Ruth Hollis = missionary in Nepal\nDon Pruitt = board"), "grace")
    assert p.zones["ruth-hollis"] == "Asia/Kathmandu"


def test_everyone_else_keeps_the_ministrys_zone():
    p = plan(_answers("Ruth Hollis = missionary in Nepal\nDon Pruitt = board"), "grace")
    assert p.zones["carol-jennings"] == "America/New_York"
    assert p.zones["don-pruitt"] == "America/New_York"
    assert set(p.zones) == {pid for pid, _n, _r in p.people}


@pytest.mark.parametrize(
    "line, zone",
    [
        ("Ruth Hollis = missionary in Nepal, ruth@example.org", "Asia/Kathmandu"),
        ("Ruth Hollis = missionary based in Kathmandu", "Asia/Kathmandu"),
        ("Ruth Hollis = missionary in Maputo", "Africa/Maputo"),
        ("Ruth Hollis = missionary in training", "America/New_York"),
        ("Ruth Hollis = missionary in the United States", "America/New_York"),
    ],
)
def test_only_a_place_that_names_one_zone_moves_a_person(line, zone):
    p = plan(_answers(line), "grace")
    assert p.zones["ruth-hollis"] == zone


def test_the_role_is_still_the_role():
    p = plan(_answers("Ruth Hollis = missionary in Nepal"), "grace")
    assert ("ruth-hollis", "Ruth Hollis", "missionary") in p.people


def test_answers_saved_before_this_change_keep_the_ministrys_zone():
    answers = _answers("Ruth Hollis = missionary in Nepal")
    for entry in answers["people"]:
        entry.pop("zone", None)  # as an answer file from before this change
    p = plan(answers, "grace")
    assert p.zones["ruth-hollis"] == "America/New_York"


def test_the_plan_shows_each_persons_zone(tmp_path):
    answers = _answers("Ruth Hollis = missionary in Nepal")
    (tmp_path / "onboarding-grace.json").write_text(json.dumps(answers))
    tools = OnboardingTools(answers_dir=tmp_path, root=tmp_path / "tenants", run_audit=False)
    shown = tools.call("onboarding_plan", {"slug": "grace"})
    people = {p["name"]: p for p in shown["plan"]["people"]}
    assert people["Ruth Hollis"]["zone"] == "Asia/Kathmandu"
    assert people["Carol Jennings"]["zone"] == "America/New_York"


def test_apply_writes_each_zone_beside_its_person(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    p = plan(_answers("Ruth Hollis = missionary in Nepal, ruth@example.org"), "grace")
    result = apply(p, root=root, run_audit=False)
    data = tomllib.loads((result.tenant_dir / "authority.toml").read_text())
    assert data["people"]["ruth-hollis"]["zone"] == "Asia/Kathmandu"
    assert data["people"]["ruth-hollis"]["email"] == "ruth@example.org"
    assert data["people"]["carol-jennings"]["zone"] == "America/New_York"
    assert set(parse_policy(data).people) == {"carol-jennings", "ruth-hollis"}
