"""The proposal lane (phase 7 row 7.18), replayed offline.

Lens 19 names the automation that would retire a repeat. This lane turns a
NAMED candidate into a proposal PR: a design note under ``docs/proposals/``
and one failing eval under ``evals/``, no implementation. The owner merges
the eval as the decision and the build lane picks the row up.

Two nights are recorded here. Night one opens the proposal and the gate
admits it. Night two sees the candidate already answered (the note is on
main, and the previous triage note says so in the bundle) and opens nothing.
No model, no key, no network: the session is a recorded set of ``Turn``
replies through the fixture adapter, and only ``git`` really runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.llm.adapters.fixture import FixtureAdapter
from core.llm.runner import Budget, ContextBundle, ContextItem, GatewayLoopRunner, Skill, run
from core.llm.skill_harness import check_note, load_contract
from core.llm.tools import FileTools, GhTool, GitTool, PytestTool, ToolAllowlist

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "audit-triage" / "SKILL.md"
CONTRACT = REPO / "skills" / "audit-triage" / "contract.toml"
PROPOSAL_CONTRACT = REPO / "skills" / "audit-triage" / "proposal-contract.toml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "audit-triage"

CANDIDATE = "a1b2c3d4"
DOC = "docs/proposals/2026-09-18-hand-check-cards.md"
EVAL = "evals/proposals/test_hand_check_cards.py"

REPORT = f"""# Audit 2026-09-18

## New since the last report

- **INFO** (recurrence) qbo unknown-clearing: 6 subjects in 30 days (6 still
  open): a class is a missing feature, not an incident; candidate: park a
  card for every hand-written check the ledger cannot explain [{CANDIDATE}]
"""

DOC_BODY = f"""# proposal: park a card for every hand-written check

Candidate: {CANDIDATE}

## Design

Lens 19 has raised the same class on six subjects inside the window. Each one
is a cleared payment with no ledger row, and the owner answers each by hand.
The automation parks one approval card per unexplained clearing.

## Failing eval

`{EVAL}` asserts that a cleared payment with no ledger row parks a card. It
fails today because nothing parks one.

## Issue

Goal: a cleared payment the ledger cannot explain parks a card.
Acceptance: the eval above passes.
Size: M. Door: two-way.
"""

EVAL_BODY = f'''"""Proposal {CANDIDATE}: fails by design until the row is built."""


def test_an_unexplained_clearing_parks_a_card():
    assert False, "proposal {CANDIDATE}: unimplemented"
'''


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _worktree(tmp_path: Path, *, merged_proposal: bool = False) -> Path:
    """What the 06:00 wrapper hands the session: a checkout off origin/main
    on its own branch. With ``merged_proposal`` the first night's note is
    already on main."""
    root = tmp_path / "worktree"
    (root / "docs" / "proposals").mkdir(parents=True)
    (root / "auditor" / "lenses").mkdir(parents=True)
    (root / "auditor" / "lenses" / "recurrence.py").write_text("# the lens\n", encoding="utf-8")
    (root / "docs" / "proposals" / "README.md").write_text("# proposals\n", encoding="utf-8")
    if merged_proposal:
        (root / DOC).write_text(DOC_BODY, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "runner@example.invalid")
    _git(root, "config", "user.name", "Runner Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "switch", "-q", "-c", "triage/2026-09-18")
    return root


def _allowlist(root: Path) -> ToolAllowlist:
    return ToolAllowlist(
        files=FileTools(root=root),
        git=GitTool(root=root, subcommands=["status", "diff", "add", "commit", "push"]),
        gh=GhTool(
            root=root,
            subcommands=["pr create"],
            gates=["eval_first", "proposal"],
            base_ref="origin/main",
        ),
        pytest=PytestTool(root=root),
    )


@pytest.fixture
def offline(monkeypatch):
    """Only git really runs: pytest reports the red eval, gh reports a PR."""
    from core.llm import tools

    original = tools.run_subprocess

    def fake_run(argv, **kw):
        if argv[:3] == ["uv", "run", "pytest"]:
            return tools.CompletedRun(1, "1 failed", "", 0.0)
        if argv[0] == "gh":
            return tools.CompletedRun(0, "https://example.invalid/pull/1\n", "", 0.0)
        return original(argv, **kw)

    monkeypatch.setattr(tools, "run_subprocess", fake_run)


def _session(root: Path, tmp_path: Path, turns: list[dict], *, notes: str = ""):
    items = [ContextItem.from_text("audit_reports/audit-2026-09-18.md", REPORT)]
    if notes:
        items.append(ContextItem.from_text("triage_notes/triage-2026-09-18.md", notes))
    return run(
        Skill.load(SKILL),
        ContextBundle(items=items),
        _allowlist(root),
        runner=GatewayLoopRunner(FixtureAdapter([json.dumps(t) for t in turns])),
        budget=Budget(max_turns=10, max_seconds=120),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts",
    )


def _night_one_turns(note: str) -> list[dict]:
    return [
        {
            "reason": "the class candidate is named; write the design note",
            "progress": "preliminary: proposing the hand-check card lane",
            "call": {"tool": "write_file", "path": DOC, "content": DOC_BODY},
        },
        {
            "reason": "the eval that fails by design",
            "progress": "preliminary: eval next",
            "call": {"tool": "write_file", "path": EVAL, "content": EVAL_BODY},
        },
        {
            "reason": "prove it fails",
            "progress": "preliminary: running it",
            "call": {"tool": "pytest", "args": [EVAL]},
        },
        {
            "reason": "open the proposal PR",
            "progress": "preliminary: opening the PR",
            "call": {
                "tool": "gh",
                "args": [
                    "pr",
                    "create",
                    "--title",
                    "proposal: hand-check cards",
                    "--body-file",
                    DOC,
                ],
            },
        },
        {"reason": "done", "note": note},
    ]


def _changed(root: Path) -> list[str]:
    argv = ["git", "status", "--porcelain", "-uall"]
    out = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=True)
    return sorted(line[3:] for line in out.stdout.splitlines() if line.strip())


# ------------------------------------------------------------------- night one


def test_a_named_candidate_becomes_one_proposal_pr(tmp_path, offline):
    root = _worktree(tmp_path)
    note = (FIXTURES / "proposal-note.md").read_text(encoding="utf-8")
    result = _session(root, tmp_path, _night_one_turns(note))
    assert result.status == "FINISHED", result.stop_reason
    assert result.refused == [], [r.reason for r in result.refused]
    created = [c for c in result.commands_run if c.tool == "gh"]
    assert len(created) == 1, "one candidate, one proposal PR"
    assert created[0].exit_code == 0


def test_the_proposal_pr_body_is_the_design_note_and_carries_its_two_sections(tmp_path, offline):
    root = _worktree(tmp_path)
    note = (FIXTURES / "proposal-note.md").read_text(encoding="utf-8")
    result = _session(root, tmp_path, _night_one_turns(note))
    (gh,) = [c for c in result.commands_run if c.tool == "gh"]
    body_file = gh.argv[gh.argv.index("--body-file") + 1]
    assert body_file == DOC
    body = (root / body_file).read_text(encoding="utf-8")
    assert check_note(body, load_contract(PROPOSAL_CONTRACT)) == []
    assert f"Candidate: {CANDIDATE}" in body


def test_the_session_changes_nothing_but_the_note_and_the_eval(tmp_path, offline):
    root = _worktree(tmp_path)
    note = (FIXTURES / "proposal-note.md").read_text(encoding="utf-8")
    _session(root, tmp_path, _night_one_turns(note))
    assert _changed(root) == [DOC, EVAL]


def test_the_note_records_the_proposal_and_keeps_the_triage_contract(tmp_path, offline):
    root = _worktree(tmp_path)
    note = (FIXTURES / "proposal-note.md").read_text(encoding="utf-8")
    result = _session(root, tmp_path, _night_one_turns(note))
    assert check_note(result.note, load_contract(CONTRACT)) == []
    assert CANDIDATE in result.note, "the note records the candidate id it answered"


# ------------------------------------------------------------------- night two


def test_a_second_night_with_the_same_candidate_opens_nothing(tmp_path, offline):
    """The recorded second night: the previous note is in the bundle, so the
    session writes its note and reaches for no tool at all."""
    root = _worktree(tmp_path, merged_proposal=True)
    yesterday = (FIXTURES / "proposal-note.md").read_text(encoding="utf-8")
    note = (FIXTURES / "second-night-note.md").read_text(encoding="utf-8")
    turns = [{"reason": "already proposed", "note": note}]
    result = _session(root, tmp_path, turns, notes=yesterday)
    assert result.status == "FINISHED", result.stop_reason
    assert result.commands_run == [], "no PR, no branch, no command"
    assert result.refused == []
    assert _changed(root) == []
    assert check_note(result.note, load_contract(CONTRACT)) == []


def test_a_second_night_that_tries_anyway_is_refused_by_the_gate(tmp_path, offline):
    """Prose is not the guard. A session that proposes the same candidate a
    second time is refused in Python, and the note says so."""
    root = _worktree(tmp_path, merged_proposal=True)
    note = (FIXTURES / "second-night-note.md").read_text(encoding="utf-8")
    turns = _night_one_turns(note)
    turns[0]["call"]["path"] = "docs/proposals/2026-09-19-hand-check-cards.md"
    turns[3]["call"]["args"][-1] = "docs/proposals/2026-09-19-hand-check-cards.md"
    result = _session(root, tmp_path, turns)
    assert [r.tool for r in result.refused] == ["gh"]
    assert result.refused[0].reason.startswith("proposal:")
    assert CANDIDATE in result.refused[0].reason
    assert [c for c in result.commands_run if c.tool == "gh"] == []
