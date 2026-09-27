"""Skill regression harness (phase 7 row 7.14).

The prose skills are executed by a Claude Code session, so nothing under
``tests/`` can run them. What CAN run in CI, with no network and no key, is
the contract each skill promises: the note it writes carries these sections
in this order, never these strings, and never a first line that still says
the run is unfinished; and the SKILL.md itself still names every section and
every guardrail marker, so an edit that renames a section fails here before
a headless run finds out at 06:00.

Fixtures under ``tests/skills/fixtures/<skill>/`` are synthetic notes: a good
one per skill that satisfies the contract and a bad one that breaks it in
named ways. No real vendor, amount, or person from the repo history appears
in them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.llm.skill_harness import (
    Contract,
    check_note,
    check_skill,
    load_contract,
)

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "skills"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SKILL_NAMES = ("audit-triage",)


def _contract(skill: str) -> Contract:
    return load_contract(SKILLS / skill / "contract.toml")


def _skill_text(skill: str) -> str:
    return (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")


def _fixture(skill: str, name: str) -> str:
    return (FIXTURES / skill / name).read_text(encoding="utf-8")


# ------------------------------------------------------------- the contract


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_contract_declares_every_field(skill):
    contract = _contract(skill)
    assert contract.name == skill
    assert contract.required_sections, "a skill without sections has no note contract"
    assert contract.banned_strings
    assert "preliminary" in [s.lower() for s in contract.first_line_never]
    assert contract.skill_must_mention


def test_audit_triage_contract_names_the_six_sections_in_skill_order():
    assert _contract("audit-triage").required_sections == [
        "New findings",
        "Root cause",
        "Actions taken",
        "Recommended actions for the owner",
        "Recurring patterns",
        "Watch list",
    ]


def test_audit_triage_never_merges():
    triage = [s.lower() for s in _contract("audit-triage").banned_strings]
    assert "gh pr merge" in triage


# ---------------------------------------------------- the build lane (7.6)


# ----------------------------------------------------------- the good notes


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_good_note_passes(skill):
    assert check_note(_fixture(skill, "good-note.md"), _contract(skill)) == []


# ------------------------------------------------------------ the bad notes


def test_bad_triage_note_fails_on_the_renamed_section_and_the_merge():
    violations = check_note(_fixture("audit-triage", "bad-note.md"), _contract("audit-triage"))
    assert any("Recurring patterns" in v and "missing" in v for v in violations), violations
    assert any("gh pr merge" in v and "banned" in v for v in violations), violations
    assert len(violations) == 2, violations


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_a_preliminary_first_line_is_an_unfinished_note(skill):
    note = "# PRELIMINARY note, still working\n\n" + _fixture(skill, "good-note.md")
    violations = check_note(note, _contract(skill))
    assert len(violations) == 1, violations
    assert "first line" in violations[0] and "preliminary" in violations[0].lower()


def test_sections_out_of_order_are_a_named_violation():
    contract = Contract(
        name="demo",
        required_sections=["Alpha", "Beta"],
        banned_strings=[],
        first_line_never=[],
        skill_must_mention=[],
    )
    note = "# Note\n\n## Beta\n\n- b\n\n## Alpha\n\n- a\n"
    violations = check_note(note, contract)
    assert len(violations) == 1, violations
    assert "Beta" in violations[0] and "order" in violations[0]


def test_banned_strings_match_case_insensitively_and_bold_headings_count():
    contract = Contract(
        name="demo",
        required_sections=["Alpha"],
        banned_strings=["Never This"],
        first_line_never=["draft"],
        skill_must_mention=[],
    )
    assert check_note("# Note\n\n**Alpha**\n\n- fine\n", contract) == []
    violations = check_note("# Note\n\n**Alpha**\n\n- never this\n", contract)
    assert len(violations) == 1 and "Never This" in violations[0], violations


# --------------------------------------------------------- the static check


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_each_skill_passes_its_static_check(skill):
    assert check_skill(_skill_text(skill), _contract(skill)) == []


def test_a_skill_edit_that_drops_a_section_fails_statically():
    """A rename of a required section in SKILL.md (every mention, the way a
    real rename lands) is caught here, before a headless run writes a note
    the wrapper and the morning brief no longer understand."""
    mutated = _skill_text("audit-triage").replace("Recurring patterns", "Repeat offenders")
    violations = check_skill(mutated, _contract("audit-triage"))
    assert violations, "the mutated skill must fail"
    assert all("Recurring patterns" in v for v in violations), violations


def test_contract_loader_rejects_an_unknown_field(tmp_path):
    bad = tmp_path / "contract.toml"
    bad.write_text('name = "x"\nrequired_sections = ["A"]\nsurprise = 1\n', encoding="utf-8")
    with pytest.raises(ValueError, match="surprise"):
        load_contract(bad)


# ------------------------------------------------- the proposal lane (7.18)


def _proposal_contract() -> Contract:
    """The triage skill's second contract: the design note it writes, which
    is also the proposal PR's body."""
    return load_contract(SKILLS / "audit-triage" / "proposal-contract.toml")


def test_proposal_contract_requires_design_failing_eval_and_issue():
    contract = _proposal_contract()
    assert contract.name == "audit-triage-proposal"
    assert contract.required_sections == ["Design", "Failing eval", "Issue"]
    assert "gh pr merge" in [s.lower() for s in contract.banned_strings]


def test_the_triage_skill_passes_the_proposal_static_check():
    assert check_skill(_skill_text("audit-triage"), _proposal_contract()) == []


def test_the_triage_skill_names_the_proposal_step_and_both_trees():
    must = _contract("audit-triage").skill_must_mention
    for marker in ("Proposal PRs", "docs/proposals/", "evals/proposals/", "Candidate:"):
        assert marker in must, marker


def test_a_skill_edit_that_drops_the_proposal_step_fails_statically():
    mutated = _skill_text("audit-triage").replace("Proposal PRs", "Nice ideas")
    violations = check_skill(mutated, _contract("audit-triage"))
    assert any("Proposal PRs" in v for v in violations), violations


def test_the_proposal_note_fixtures_are_valid_triage_notes():
    triage = _contract("audit-triage")
    assert check_note(_fixture("audit-triage", "proposal-note.md"), triage) == []
    assert check_note(_fixture("audit-triage", "second-night-note.md"), triage) == []
