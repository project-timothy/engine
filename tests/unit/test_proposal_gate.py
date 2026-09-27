"""The proposal gate (phase 7 row 7.18).

A PROPOSAL PR carries a design note and one failing eval, and nothing else.
The rule that makes it safe to merge is Python, not prose: ``ToolGate``
admits ``gh pr create`` under the ``proposal`` gate only when the worktree
holds exactly one new design note under ``docs/proposals/``, exactly one new
eval under ``evals/``, no other change at all, and a ``pytest`` run in this
session that named that eval and FAILED.

The failing half is the point. An eval that fails by design is the decision
the owner merges, and it lives under the top-level ``evals/`` tree, which is
outside ``testpaths``: merging red here never turns the suite red.

``eval_first`` is not weakened by any of this. A diff that touches source is
never proposal-shaped, so it still needs a new test file and a GREEN suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.llm.tools import (
    FileTools,
    Gh,
    GhTool,
    PytestTool,
    ToolAllowlist,
    ToolGate,
    WriteFile,
)
from core.llm.tools import (
    Pytest as PytestCall,
)
from core.llm.transcript import Transcript

DOC = "docs/proposals/2026-09-18-hand-check-cards.md"
EVAL = "evals/proposals/test_hand_check_cards.py"
CANDIDATE = "a1b2c3d4"

DOC_BODY = f"""# proposal: park a card for every hand-written check

Candidate: {CANDIDATE}

## Design

The recurrence lens has named the same automation on six nights.

## Failing eval

`{EVAL}` asserts the card exists and fails today.

## Issue

Goal: park the card. Acceptance: the eval passes.
"""

EVAL_BODY = """def test_the_card_is_parked():
    assert False, "proposal a1b2c3d4: unimplemented by design"
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    (root / "docs" / "proposals").mkdir(parents=True)
    (root / "auditor" / "lenses").mkdir(parents=True)
    (root / "auditor" / "lenses" / "recurrence.py").write_text("# a lens\n", encoding="utf-8")
    (root / "docs" / "proposals" / "README.md").write_text("# proposals\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "runner@example.invalid")
    _git(root, "config", "user.name", "Runner Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "switch", "-q", "-c", "triage/2026-09-18")
    return root


def _gate(root: Path, tmp_path: Path, gates: list[str]) -> ToolGate:
    allowlist = ToolAllowlist(
        files=FileTools(root=root),
        gh=GhTool(root=root, subcommands=["pr create"], gates=gates, base_ref="origin/main"),
        pytest=PytestTool(root=root),
    )
    return ToolGate(allowlist, transcript=Transcript(tmp_path / "t.jsonl"))


@pytest.fixture
def fake_pytest(monkeypatch):
    """pytest never really runs here: the exit code is the fixture's knob."""
    from core.llm import tools

    original = tools.run_subprocess
    state = {"code": 1}

    def fake_run(argv, **kw):
        if argv[:3] == ["uv", "run", "pytest"]:
            return tools.CompletedRun(state["code"], "", "", 0.0)
        return original(argv, **kw)

    monkeypatch.setattr(tools, "run_subprocess", fake_run)
    return state


def _create() -> Gh:
    return Gh(args=["pr", "create", "--title", "proposal: park a card", "--body-file", DOC])


def _write_proposal(gate: ToolGate, *, doc: str = DOC_BODY) -> None:
    assert gate.call(WriteFile(path=DOC, content=doc), turn=1).ok
    assert gate.call(WriteFile(path=EVAL, content=EVAL_BODY), turn=2).ok


# ---------------------------------------------------------------- the happy shape


def test_a_design_note_and_one_failing_eval_open_the_gate(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(_create())
    assert verdict.allowed, verdict.reason


def test_the_same_shape_under_eval_first_alone_is_refused(tmp_path, fake_pytest):
    """The proposal lane is opt-in per lane: without the gate declared, a
    red eval never opens a PR, because eval_first wants a green suite."""
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first"])
    _write_proposal(gate)
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed
    assert verdict.reason.startswith("eval_first:")


# ------------------------------------------------------------------- the refusals


def test_a_proposal_that_also_changes_source_is_refused(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    assert gate.call(
        WriteFile(path="auditor/lenses/recurrence.py", content="# implemented\n"), turn=3
    ).ok
    assert gate.call(PytestCall(args=[EVAL]), turn=4).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed
    assert verdict.reason.startswith("proposal:")
    assert "auditor/lenses/recurrence.py" in verdict.reason


def test_an_eval_that_passes_is_refused(tmp_path, fake_pytest):
    """A proposal whose eval passes has implemented something, or asserts
    nothing. Either way it is not a proposal."""
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    fake_pytest["code"] = 0
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 0
    verdict = gate.check(_create())
    assert not verdict.allowed and "fails by design" in verdict.reason


def test_a_pytest_that_never_named_the_eval_is_refused(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    assert gate.call(PytestCall(args=["tests"]), turn=3).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed and EVAL in verdict.reason


def test_a_write_after_the_pytest_closes_the_gate_again(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    assert gate.call(WriteFile(path=DOC, content=DOC_BODY + "\nmore\n"), turn=4).ok
    verdict = gate.check(_create())
    assert not verdict.allowed and "since the last write" in verdict.reason


def test_two_evals_are_refused(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    assert gate.call(WriteFile(path="evals/proposals/test_second.py", content=EVAL_BODY), turn=3).ok
    assert gate.call(PytestCall(args=[EVAL]), turn=4).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed and "one failing eval" in verdict.reason


def test_a_note_without_the_two_sections_is_refused(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate, doc=f"# proposal\n\nCandidate: {CANDIDATE}\n\n## Design\n\nno eval.\n")
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed and "Failing eval" in verdict.reason


def test_a_note_without_a_candidate_id_is_refused(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate, doc=DOC_BODY.replace(f"Candidate: {CANDIDATE}", "Candidate:"))
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed and "Candidate:" in verdict.reason


def test_the_pr_body_must_be_the_design_note(tmp_path, fake_pytest):
    """The note IS the body: what the owner reads in the PR is what merges
    into the repo, so there is no second copy to drift."""
    root = _repo(tmp_path)
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate)
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(Gh(args=["pr", "create", "--fill"]))
    assert not verdict.allowed and "--body-file" in verdict.reason


# ------------------------------------------------------------------- once only


def test_a_candidate_that_already_has_a_proposal_is_refused(tmp_path, fake_pytest):
    """The second night. The first night's note is on main now, so the
    candidate id is already answered and nothing opens."""
    root = _repo(tmp_path)
    (root / "docs" / "proposals" / "2026-09-18-hand-check-cards.md").write_text(
        DOC_BODY, encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the merged proposal")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    second = "docs/proposals/2026-09-19-hand-check-cards.md"
    assert gate.call(WriteFile(path=second, content=DOC_BODY), turn=1).ok
    assert gate.call(WriteFile(path=EVAL, content=EVAL_BODY), turn=2).ok
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(
        Gh(args=["pr", "create", "--title", "proposal: again", "--body-file", second])
    )
    assert not verdict.allowed
    assert CANDIDATE in verdict.reason and "already has a proposal" in verdict.reason


def test_rewriting_a_merged_note_is_refused(tmp_path, fake_pytest):
    root = _repo(tmp_path)
    (root / "docs" / "proposals" / "2026-09-18-hand-check-cards.md").write_text(
        DOC_BODY, encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the merged proposal")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    gate = _gate(root, tmp_path, ["eval_first", "proposal"])
    _write_proposal(gate, doc=DOC_BODY + "\nsecond thoughts\n")
    assert gate.call(PytestCall(args=[EVAL]), turn=3).exit_code == 1
    verdict = gate.check(_create())
    assert not verdict.allowed and "written once" in verdict.reason
