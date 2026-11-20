"""Naming yourself in setup's people answer makes no second you (board item
t-setup-self-twice; the finding of 2026-10-23).

"Lydia set Harvest up ('your name' = Lydia Brooks) and also listed 'Lydia
Brooks = finance director, lydia@...' under people; setup wrote lydia-brooks
AND lydia-brooks-2 (the address on the second). Setup should recognize the
person setting up and merge the line into them (address, role) instead of
adding a twin."

- A line in the people answer that names the person setting up (the same
  name, compared the way setup already compares names) merges into them:
  its address, and its place. No second person is written.
- Two different people who share a name are two only when the answers say
  so plainly: a different address given for each. A line with the setter's
  name and another role, and no address to tell them apart, is asked again,
  the way setup asks today.
- Every other answer writes exactly what it wrote before (the pin by hash
  in the pull request)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from core.onboarding import OnboardingError, apply, next_question, plan, record

HARVEST = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "harvest"

LYDIA = {
    "who": "Our ministry organization",
    "legal_name": "Harvest Fields Mission, Inc.",
    "your_name": "Lydia Brooks",
    "your_role": "finance",
    "timezone": "Central",
    "people": "Lydia Brooks = finance director, lydia@harvestfields.example.org\n"
    "Ann Lee = team-leader\nSam Ortiz = missionary",
    "looks_after": "Ann Lee = Sam Ortiz",
}


REST = "\nAnn Lee = team-leader\nSam Ortiz = missionary"


def _answers(**over) -> dict:
    values = {**LYDIA, **over}
    answers: dict = {"pack": str(HARVEST)}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return root


def test_lydia_listed_under_people_is_lydia_once(world):
    p = plan(_answers(), "harvest")
    lydias = [row for row in p.people if row[1] == "Lydia Brooks"]
    assert lydias == [("lydia-brooks", "Lydia Brooks", "finance")]
    assert p.emails["lydia-brooks"] == "lydia@harvestfields.example.org"
    apply(p, root=world, run_audit=False)
    people = tomllib.loads((world / "harvest" / "authority.toml").read_text())["people"]
    assert "lydia-brooks-2" not in people
    assert people["lydia-brooks"]["email"] == "lydia@harvestfields.example.org"


def test_the_name_is_compared_the_way_setup_compares_names():
    p = plan(
        _answers(people="  lydia   BROOKS = finance, lydia@harvestfields.example.org" + REST),
        "harvest",
    )
    assert [row[0] for row in p.people] == ["lydia-brooks", "ann-lee", "sam-ortiz"]
    assert p.emails == {"lydia-brooks": "lydia@harvestfields.example.org"}


def test_her_own_address_said_twice_is_one_lydia():
    p = plan(_answers(your_name="Lydia Brooks, lydia@harvestfields.example.org"), "harvest")
    assert [row[0] for row in p.people].count("lydia-brooks") == 1
    assert "lydia-brooks-2" not in [row[0] for row in p.people]


def test_her_place_on_the_line_is_hers():
    p = plan(_answers(people="Lydia Brooks = finance in Kenya" + REST), "harvest")
    assert [row[0] for row in p.people] == ["lydia-brooks", "ann-lee", "sam-ortiz"]
    assert p.zones["lydia-brooks"] == "Africa/Nairobi"


def test_two_lydias_with_their_own_addresses_are_two_people():
    p = plan(
        _answers(
            your_name="Lydia Brooks, lydia@harvestfields.example.org",
            people="Lydia Brooks = missionary, lbrooks@mail.example.org" + REST,
        ),
        "harvest",
    )
    assert ("lydia-brooks", "Lydia Brooks", "finance") in p.people
    assert ("lydia-brooks-2", "Lydia Brooks", "missionary") in p.people
    assert p.emails == {
        "lydia-brooks": "lydia@harvestfields.example.org",
        "lydia-brooks-2": "lbrooks@mail.example.org",
    }


@pytest.mark.parametrize(
    "line",
    [
        "Lydia Brooks = missionary",
        "Lydia Brooks = missionary, lydia@harvestfields.example.org",
    ],
)
def test_her_name_with_another_role_and_nothing_to_tell_them_apart_is_asked(line):
    answers: dict = {"pack": str(HARVEST)}
    while (q := next_question(answers)) is not None and q["id"] != "people":
        answers = record(answers, q["id"], LYDIA.get(q["id"], q.get("default", "")))
    with pytest.raises(OnboardingError, match="Lydia Brooks is the name you gave as yours"):
        record(answers, "people", line)


def test_the_words_carry_no_machinery_and_no_em_dash():
    from core.onboarding import SAID

    for key, words in SAID.items():
        assert "—" not in words, key
        for banned in ("role", "scope", "permission", "ledger", "tenant", "_", "#", "delve"):
            assert banned not in words.lower(), (key, banned)
