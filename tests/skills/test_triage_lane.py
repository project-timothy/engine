"""The triage lane on the runner (phase 7 row 7.17).

Row 7.17's acceptance, offline: the note contract the morning brief and the
wrapper depend on is unchanged, and the eval-first rule is now PYTHON, not
prose. A session that reaches for ``gh pr create`` with no new or changed
test file is refused by :class:`~core.llm.tools.ToolGate`, the refusal is
recorded, and the note names it.

The session here is a recorded set of ``Turn`` replies replayed through the
fixture adapter: no model, no key, no network. What it proves is the gate
and the contract, which is what a 06:00 run depends on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.llm.adapters.fixture import FixtureAdapter
from core.llm.runner import Budget, ContextBundle, ContextItem, GatewayLoopRunner, Skill, run
from core.llm.skill_harness import check_note, check_transcript, load_contract
from core.llm.tools import FileTools, GhTool, GitTool, PytestTool, ToolAllowlist

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "audit-triage" / "SKILL.md"
CONTRACT = REPO / "skills" / "audit-triage" / "contract.toml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "audit-triage"

REFUSED_NOTE = FIXTURES / "refused-pr-note.md"


def _repo(tmp_path: Path) -> Path:
    """A worktree-shaped root: a git repo with one source file and no test."""
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "lens.py").write_text("# a lens with a bug\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "s"],
        check=True,
    )
    return root


def _allowlist(root: Path) -> ToolAllowlist:
    """What the 06:00 lane hands the runner: write, git, gh behind the gate,
    pytest. ``base_ref`` is the branch this fixture repo actually has."""
    return ToolAllowlist(
        files=FileTools(root=root),
        git=GitTool(root=root, subcommands=["status", "diff", "add", "commit", "push"]),
        gh=GhTool(root=root, subcommands=["pr create"], gates=["eval_first"], base_ref="HEAD"),
        pytest=PytestTool(root=root),
    )


def _session(tmp_path: Path, turns: list[dict]):
    root = _repo(tmp_path)
    return run(
        Skill.load(SKILL),
        ContextBundle(items=[ContextItem.from_text("audit report", "one CRITICAL finding")]),
        _allowlist(root),
        runner=GatewayLoopRunner(FixtureAdapter([json.dumps(t) for t in turns])),
        budget=Budget(max_turns=6, max_seconds=120),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts",
    )


def _turns(note: str) -> list[dict]:
    return [
        {
            "reason": "the fix is one line; open the PR",
            "progress": "diagnosed",
            "call": {"tool": "gh", "args": ["pr", "create", "--fill"]},
        },
        {"reason": "the gate refused; record it", "progress": "refused", "note": note},
    ]


# ------------------------------------------------- the eval-first gate in Python


def test_a_session_that_opens_a_pr_with_no_test_file_is_refused(tmp_path):
    result = _session(tmp_path, _turns(REFUSED_NOTE.read_text(encoding="utf-8")))
    assert result.status == "FINISHED", result.stop_reason
    assert [r.tool for r in result.refused] == ["gh"]
    assert result.refused[0].reason.startswith("eval_first:")
    assert "no new or changed test file" in result.refused[0].reason
    assert result.commands_run == [], "a refused call never runs"


def test_the_note_names_the_refusal(tmp_path):
    """The session is told the reason and the note carries it: a morning
    reader must see WHY no PR exists without opening a transcript."""
    result = _session(tmp_path, _turns(REFUSED_NOTE.read_text(encoding="utf-8")))
    assert "eval_first" in result.note
    assert check_note(result.note, load_contract(CONTRACT)) == []


def test_the_refusal_is_on_the_transcript_and_the_contract_still_passes(tmp_path):
    result = _session(tmp_path, _turns(REFUSED_NOTE.read_text(encoding="utf-8")))
    assert check_transcript(result.transcript_path, load_contract(CONTRACT)) == []
    events = [json.loads(line) for line in result.transcript_path.read_text().splitlines()]
    assert any(e["event"] == "refused" and e["tool"] == "gh" for e in events)


def test_a_note_that_still_says_preliminary_is_an_unfinished_run(tmp_path):
    """The wrapper's rule, now in Python: run() marks it FAILED so no
    half-finished note is ever installed as the day's triage."""
    note = "# Triage 2026-09-17 (preliminary)\n\n" + REFUSED_NOTE.read_text(encoding="utf-8")
    result = _session(tmp_path, _turns(note))
    assert result.status == "FAILED"
    assert result.stop_reason == "unfinished"


# ---------------------------------------------------- the skill file's new home


def test_the_skill_lives_at_the_repo_root():
    """Row 7.17: `skills/audit-triage/SKILL.md` is the file. The tenant's old
    path was a symlink to it until the tenant left this repository
    (2026-09-22); the host installer now links ~/.claude/skills to this copy
    directly, so nothing in the engine points back into a tenant folder."""
    assert SKILL.is_file()
    assert CONTRACT.is_file()
    shipped = {"demo", "_templates"}
    real = [
        p.parent.name
        for p in (REPO / "tenants").glob("*/tenant.toml")
        if p.parent.name not in shipped
    ]
    assert real == [], f"a real tenant folder is back in the engine: {real}"


@pytest.mark.parametrize("marker", ["eval_first", "context bundle", "final message"])
def test_the_skill_tells_the_session_how_the_runner_works(marker):
    assert marker in SKILL.read_text(encoding="utf-8")


# ------------------------------------------------ the hand-run fixture skill


def test_the_hand_run_fixture_skill_loads_and_asks_for_no_tools():
    """Row 7.17 part A: the skill the first live hand run executed
    (docs/runner-design.md, "First hand run 2026-09-17"). It stays in the
    repo so the run can be repeated on any host with the seat."""
    skill = Skill.load(Path(__file__).resolve().parent / "fixtures" / "hand-run" / "SKILL.md")
    assert skill.name == "hand-run"
    assert skill.description
    assert "final message" in skill.body
    assert "preliminary" in skill.body


# ------------------------------------- the cost cut (owner, 2026-10-03)
# Eleven mornings measured: the session went off-report and built PRs most
# nights, its notes grew from 17.7K to 47K characters (each fed to the next
# run), and 5 to 18 commands a night were refused for shell operators.


def test_the_skill_scopes_the_session_to_the_nights_report():
    text = SKILL.read_text(encoding="utf-8")
    assert "Tonight's report is the whole job" in text
    assert "off-report:" in text


def test_the_skill_caps_the_note():
    assert "6,000 characters" in SKILL.read_text(encoding="utf-8")


def test_the_skill_states_the_one_argv_rule_up_front():
    assert "One plain argv per call" in SKILL.read_text(encoding="utf-8")
