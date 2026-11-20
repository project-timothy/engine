"""Setup writes each person's name into authority.toml (engine #603 hardening, 2026-11-12).

#603 matches a card's person by id or by an explicit ``name`` on their
[people] entry, and fails closed when a card names nobody and the decider's
entry carries no name. Setup and ``engine people add`` wrote the name only as
a comment above the table, so every box they made fell back. Now each entry
setup or the roster writes carries ``name = "..."`` in the person's own words,
so "nobody approves their own" never has to fall back.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from core.authority import parse_policy
from core.onboarding import apply, next_question, plan, record
from core.onboarding.roster import add_people


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return root


def _onboard(root: Path, slug: str, values: dict) -> dict:
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(plan(answers, slug), root=root, run_audit=False)
    return tomllib.loads((root / slug / "authority.toml").read_text(encoding="utf-8"))


GRACE = {
    "who": "nonprofit-small",
    "legal_name": "Grace Fellowship",
    "your_name": "Carol Jennings",
    "your_role": "treasurer",
    "people": "Mark Ellison = pastor\nRuth O'Hara-Hollis = missionary\nDon Pruitt = board",
}


def test_every_person_setup_writes_carries_their_name(world):
    data = _onboard(world, "grace", GRACE)
    names = {pid: entry.get("name") for pid, entry in data["people"].items()}
    assert names["carol-jennings"] == "Carol Jennings"
    assert names["mark-ellison"] == "Mark Ellison"
    assert names["don-pruitt"] == "Don Pruitt"
    # A name with an apostrophe and a hyphen survives as said.
    assert "Ruth O'Hara-Hollis" in names.values()
    assert all(names.values()), names
    # The policy reads them, so a card naming "Carol Jennings" is Carol's own.
    policy = parse_policy(data)
    assert policy.people["carol-jennings"].full_name == "Carol Jennings"


def test_a_solo_ministrys_missionary_and_reviewer_carry_their_names(world):
    data = _onboard(
        world,
        "solo",
        {
            "who": "nonprofit-solo",
            "legal_name": "Teaching Example Inc.",
            "your_name": "Sam Field",
            "reviewer": "Dee Reviewer, dee@example.org",
        },
    )
    assert {e.get("name") for e in data["people"].values()} == {"Sam Field", "Dee Reviewer"}


def test_people_added_from_a_list_carry_their_names(world):
    _onboard(world, "grace", GRACE)
    add_people("grace", "Ivy Chen = missionary, ivy@example.org\n", tenants_root=world)
    data = tomllib.loads((world / "grace" / "authority.toml").read_text(encoding="utf-8"))
    assert data["people"]["ivy-chen"]["name"] == "Ivy Chen"
