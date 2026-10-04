"""The runner (phase 7 row 7.16, docs/runner-design.md).

One entry point, ``core.llm.runner.run``, two runners: the in-repo loop over
the gateway (driven here by the fixture adapter returning canned ``Turn``
replies in sequence) and the Claude Agent SDK runner (driven by a fake
stream replaying a recorded message list, no subprocess, no network). The
suite pins what the design note calls identical between runners: the tool
gate and its refusals, the transcript and its redaction, the budget clock,
``files_changed`` from the worktree hash, and the inputs digest.

The file is ``test_llm_runner.py`` because ``tests/unit/test_runner.py``
already belongs to the engine's job runner (``core.engine.runner``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.llm import Pricing, Usage
from core.llm.adapters import claude_sdk
from core.llm.adapters.claude_sdk import ClaudeAgentSdkRunner, sdk_call_to_tool_call
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.runner import (
    Budget,
    ContextBundle,
    ContextItem,
    GatewayLoopRunner,
    Skill,
    SkillFormatError,
    Turn,
    inputs_digest,
    run,
)
from core.llm.tools import (
    FileTools,
    Gh,
    GhTool,
    Git,
    GitTool,
    Pytest,
    PytestTool,
    ReadFile,
    Shell,
    ShellTool,
    ToolAllowlist,
    ToolGate,
    WriteFile,
    reduced_env,
)
from core.llm.transcript import Transcript, read_events, redact, turns_of

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
SKILL_PATH = FIXTURES / "skills" / "edit-and-run" / "SKILL.md"
LOOP_TURNS = json.loads((FIXTURES / "runner_turns_edit_and_run.json").read_text())
SDK_STREAM = json.loads((FIXTURES / "sdk_stream_edit_and_run.json").read_text())
NOTE = "# Session note\n\n## What changed\n\n- wrote notes/hello.txt\n- ran echo ok\n"


# ------------------------------------------------------------- helpers


def _turn_replies(turns=LOOP_TURNS) -> list[str]:
    return [json.dumps(t) for t in turns]


def _bundle() -> ContextBundle:
    return ContextBundle(
        items=[
            ContextItem.from_text("issue", "Write hello and run echo."),
            ContextItem.from_text("rules", "Never merge."),
        ]
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    (root / "README.md").write_text("fixture worktree\n", encoding="utf-8")
    return root


def _allowlist(root: Path, **overrides) -> ToolAllowlist:
    fields = {
        "files": FileTools(root=root),
        "shell": ShellTool(root=root, argv_prefixes=[["echo"]]),
    }
    fields.update(overrides)
    return ToolAllowlist(**fields)


def _budget(**kw) -> Budget:
    base = {"max_turns": 10, "max_seconds": 600}
    base.update(kw)
    return Budget(**base)


def _run(tmp_path: Path, adapter, *, allowlist=None, budget=None, pricing=None, **kw):
    root = allowlist.roots()[0] if allowlist else _root(tmp_path)
    allowlist = allowlist or _allowlist(root)
    return run(
        Skill.load(SKILL_PATH),
        _bundle(),
        allowlist,
        runner=GatewayLoopRunner(adapter, pricing=pricing),
        budget=budget or _budget(),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts",
        **kw,
    )


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout


def _git_repo(tmp_path: Path) -> Path:
    """A worktree-shaped repo on branch lane/x with origin/main pointing at
    its first commit, so eval_first's diff against the base has a target."""
    root = _root(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "runner@example.invalid")
    _git(root, "config", "user.name", "Runner Test")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "switch", "-q", "-c", "lane/x")
    return root


def _gate(allowlist: ToolAllowlist, tmp_path: Path) -> ToolGate:
    return ToolGate(allowlist, transcript=Transcript(tmp_path / "t.jsonl"))


# ------------------------------------------------------------- the skill


def test_skill_load_parses_flat_frontmatter_and_hashes_the_file():
    skill = Skill.load(SKILL_PATH)
    assert skill.name == "edit-and-run"
    assert skill.description.startswith("Fixture skill for the runner tests")
    assert skill.frontmatter["license"] == "none"
    assert skill.body.startswith("# Edit and run")
    assert len(skill.sha256) == 64
    assert skill.path == SKILL_PATH


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: x\ndescription: y\ntools:\n  - Read\n",
        "name: x\ndescription: y\nmeta:\n  nested: true\n",
        "name: x\ndescription: y\nnotes: |\n  block\n",
        "name: x\n",
        "description only: no name\n",
    ],
)
def test_skill_load_rejects_nested_list_block_or_incomplete_frontmatter(tmp_path, frontmatter):
    path = tmp_path / "SKILL.md"
    path.write_text(f"---\n{frontmatter}---\n\nbody\n", encoding="utf-8")
    with pytest.raises(SkillFormatError):
        Skill.load(path)


# ----------------------------------------------------- the loop runner


def test_loop_runner_refuses_a_call_outside_the_allowlist_records_it_and_continues(tmp_path):
    """Row acceptance clause 1: the shell tool is not in the allowlist, the
    model asks for it anyway, the gate refuses, the refusal goes back to the
    model as a failed ToolResult, and the session still finishes."""
    root = _root(tmp_path)
    allowlist = ToolAllowlist(files=FileTools(root=root))
    adapter = FixtureAdapter(_turn_replies(), usage=Usage(10, 5))
    result = _run(tmp_path, adapter, allowlist=allowlist)

    assert result.status == "FINISHED"
    assert result.stop_reason == "note"
    assert result.note == NOTE
    assert len(result.refused) == 1
    refusal = result.refused[0]
    assert refusal.tool == "shell"
    assert refusal.turn == 2
    assert "not in the allowlist" in refusal.reason
    assert result.commands_run == []
    # The model saw the refusal as the tool result of turn 2.
    third_call = adapter.calls[2]
    tool_result = json.loads(third_call.messages[-2].content)
    assert tool_result["ok"] is False
    assert tool_result["tool"] == "shell"
    assert "not in the allowlist" in tool_result["refused_reason"]
    events = [e["event"] for e in read_events(result.transcript_path)]
    assert "refused" in events


def test_fixture_skill_completes_under_the_loop_runner_with_a_full_result(tmp_path):
    """Row acceptance clause 2 (loop half) and clause 3: one file edited, one
    command run, and the result carries all of it."""
    root = _root(tmp_path)
    adapter = FixtureAdapter(_turn_replies(), usage=Usage(100, 50))
    pricing = Pricing(input_usd_per_mtok=Decimal("3"), output_usd_per_mtok=Decimal("15"))
    result = _run(tmp_path, adapter, allowlist=_allowlist(root), pricing=pricing)

    assert result.status == "FINISHED"
    assert result.note == NOTE
    assert (root / "notes" / "hello.txt").read_text() == "hello\n"
    [change] = result.files_changed
    assert change.path == "notes/hello.txt"
    assert change.sha256_before is None
    assert len(change.sha256_after) == 64
    [command] = result.commands_run
    assert command.tool == "shell"
    assert command.argv == ["echo", "ok"]
    assert command.cwd == str(root)
    assert command.exit_code == 0
    assert result.refused == []
    assert result.turns == 3
    assert result.input_tokens == 300
    assert result.output_tokens == 150
    assert result.usd == Decimal("0.00315")
    assert result.wall_seconds >= 0
    assert result.model == "policy-chosen-model"
    assert result.runner == "gateway_loop"
    assert len(result.inputs_digest) == 16
    assert result.transcript_path == tmp_path / "transcripts" / f"{result.inputs_digest}.jsonl"
    assert result.transcript_path.exists()
    assert result.prior_transcript is False

    events = read_events(result.transcript_path)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start"
    assert kinds[1] == "prompt"
    assert kinds[-1] == "run_end"
    assert kinds.count("turn") == 3
    assert kinds.count("tool_call") == 2
    assert kinds.count("tool_result") == 2
    # The tool manual only names tools the allowlist admits.
    system_text = adapter.calls[0].system_text()
    assert "write_file" in system_text and "shell" in system_text
    manual = system_text.split("## Tools")[-1].split("Reply with ONLY")[0]
    assert '{"tool": "git"' not in manual and '{"tool": "gh"' not in manual


def test_a_loop_transcript_replays_to_the_same_note_and_the_same_digest(tmp_path):
    """turn.raw is what a replay feeds back: seed the fixture adapter with
    the recorded turns and the session lands on the same note and key."""
    root = _root(tmp_path)
    first = _run(tmp_path, FixtureAdapter(_turn_replies()), allowlist=_allowlist(root))
    recorded = turns_of(first.transcript_path)
    assert len(recorded) == 3

    root2 = tmp_path / "second"
    root2.mkdir()
    (root2 / "README.md").write_text("fixture worktree\n", encoding="utf-8")
    second = run(
        Skill.load(SKILL_PATH),
        _bundle(),
        _allowlist(root2),
        runner=GatewayLoopRunner(FixtureAdapter(recorded)),
        budget=_budget(),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts2",
    )
    assert second.status == "FINISHED"
    assert second.note == first.note
    assert (root2 / "notes" / "hello.txt").read_text() == "hello\n"


def test_a_rerun_with_the_same_inputs_reports_the_prior_transcript(tmp_path):
    root = _root(tmp_path)
    first = _run(tmp_path, FixtureAdapter(_turn_replies()), allowlist=_allowlist(root))
    second = _run(tmp_path, FixtureAdapter(_turn_replies()), allowlist=_allowlist(root))
    assert first.inputs_digest == second.inputs_digest
    assert second.prior_transcript is True
    assert second.status == "FINISHED"


def test_two_invalid_turns_end_the_run_failed_validation(tmp_path):
    adapter = FixtureAdapter(["not json at all", '{"call": null, "note": null}'])
    result = _run(tmp_path, adapter)
    assert result.status == "FAILED"
    assert result.stop_reason == "validation"
    assert len(adapter.calls) == 2


def test_a_note_whose_first_line_says_preliminary_is_not_finished(tmp_path):
    turns = [{"reason": "early", "note": "# PRELIMINARY session note\n\nnot done\n"}]
    result = _run(tmp_path, FixtureAdapter(_turn_replies(turns)))
    assert result.status == "FAILED"
    assert result.stop_reason == "unfinished"
    assert result.note.startswith("# PRELIMINARY")


# ------------------------------------------------------ the SDK runner


def _sdk_messages(mod, root: Path):
    """The recorded stream as the SDK's own dataclasses."""
    out = []
    for item in SDK_STREAM:
        if item["type"] == "assistant":
            blocks = [mod.TextBlock(text=item["text"])]
            blocks += [
                mod.ToolUseBlock(id=t["id"], name=t["name"], input=t["input"])
                for t in item["tool_uses"]
            ]
            out.append(
                mod.AssistantMessage(
                    content=blocks, model="policy-chosen-model", usage=item["usage"]
                )
            )
        elif item["type"] == "tool_result":
            out.append(
                mod.UserMessage(
                    content=[
                        mod.ToolResultBlock(
                            tool_use_id=item["tool_use_id"],
                            content=item["content"],
                            is_error=item["is_error"],
                        )
                    ]
                )
            )
        else:
            out.append(
                mod.ResultMessage(
                    subtype=item["subtype"],
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=item["is_error"],
                    num_turns=item["num_turns"],
                    session_id="fixture",
                    total_cost_usd=item["total_cost_usd"],
                    usage=item["usage"],
                    result=item["result"],
                )
            )
    return out


class _FakeStream:
    """Stands in for the SDK's query(): replays the recorded messages and,
    like the CLI, runs the PreToolUse hook and the permission callback for
    every tool use, applies the effect of an allowed Write, and answers a
    denied call with an error tool result. Records what it was asked."""

    def __init__(self, mod, root: Path) -> None:
        self.mod = mod
        self.root = root
        self.prompts: list[str] = []
        self.options = None
        self.hook_calls: list[str] = []
        self.permission_calls: list[str] = []

    def __call__(self, prompt, options):
        self.prompts.append(prompt)
        self.options = options
        return self._gen(options)

    async def _gen(self, options):
        mod = self.mod
        results = {
            m.content[0].tool_use_id: m
            for m in _sdk_messages(mod, self.root)
            if isinstance(m, mod.UserMessage)
        }
        for message in _sdk_messages(mod, self.root):
            if isinstance(message, mod.UserMessage):
                continue  # replayed right after its tool use below
            yield message
            if not isinstance(message, mod.AssistantMessage):
                continue
            for block in message.content:
                if not isinstance(block, mod.ToolUseBlock):
                    continue
                hook = options.hooks["PreToolUse"][0].hooks[0]
                self.hook_calls.append(block.name)
                out = await hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": block.name,
                        "tool_input": block.input,
                        "tool_use_id": block.id,
                    },
                    block.id,
                    None,
                )
                specific = out.get("hookSpecificOutput", {})
                denied = specific.get("permissionDecision") == "deny"
                reason = specific.get("permissionDecisionReason", "")
                if not denied:
                    self.permission_calls.append(block.name)
                    verdict = await options.can_use_tool(
                        block.name, block.input, mod.ToolPermissionContext(tool_use_id=block.id)
                    )
                    denied = verdict.behavior == "deny"
                    reason = getattr(verdict, "message", "")
                if denied:
                    yield mod.UserMessage(
                        content=[
                            mod.ToolResultBlock(tool_use_id=block.id, content=reason, is_error=True)
                        ]
                    )
                    continue
                if block.name == "Write":
                    target = self.root / block.input["file_path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(block.input["content"], encoding="utf-8")
                yield results[block.id]


def test_fixture_skill_completes_under_the_sdk_runner_against_a_recorded_stream(
    tmp_path, monkeypatch
):
    """Row acceptance clause 2 (SDK half): the same skill, the same worktree
    truth, the gate reached through the SDK's hook and permission callback,
    a disallowed tool refused on the way, tokens and usd from the result."""
    mod = pytest.importorskip("claude_agent_sdk")
    root = _root(tmp_path)
    fake = _FakeStream(mod, root)
    monkeypatch.setattr(claude_sdk, "stream", fake)

    result = run(
        Skill.load(SKILL_PATH),
        _bundle(),
        _allowlist(root),
        runner=ClaudeAgentSdkRunner(),
        budget=_budget(max_turns=8),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts",
    )

    assert result.status == "FINISHED"
    assert result.note == NOTE
    assert result.runner == "claude_agent_sdk"
    assert (root / "notes" / "hello.txt").read_text() == "hello\n"
    [change] = result.files_changed
    assert change.path == "notes/hello.txt" and change.sha256_before is None
    [command] = result.commands_run
    assert command.tool == "shell" and command.argv == ["echo", "ok"] and command.exit_code == 0
    [refusal] = result.refused
    assert refusal.tool == "WebFetch"
    assert "not in the allowlist" in refusal.reason
    assert result.turns == 4
    assert result.input_tokens == 400
    assert result.output_tokens == 120
    assert result.usd == Decimal("0.0123")
    assert result.transcript_path.exists()

    # Every tool use went through the gate in the hook; the permission
    # callback was asked only for what the hook let through.
    assert fake.hook_calls == ["Write", "Bash", "WebFetch"]
    assert fake.permission_calls == ["Write", "Bash"]
    options = fake.options
    assert options.allowed_tools == []  # nothing pre-approved: the callback is never shadowed
    assert "WebFetch" in options.disallowed_tools
    assert "Bash" not in options.disallowed_tools
    assert options.cwd == str(root)
    assert options.max_turns == 8
    assert options.max_buffer_size == 32 * 1024 * 1024
    assert options.model == "policy-chosen-model"
    assert options.setting_sources == []
    assert options.system_prompt == Skill.load(SKILL_PATH).body
    assert fake.prompts == [_bundle().render()]

    kinds = [e["event"] for e in read_events(result.transcript_path)]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert kinds.count("turn") == 4
    assert "refused" in kinds


def test_sdk_runner_skips_with_a_precondition_when_the_extra_is_absent(tmp_path, monkeypatch):
    def missing():
        raise ModuleNotFoundError("No module named 'claude_agent_sdk'")

    monkeypatch.setattr(claude_sdk, "sdk", missing)
    result = run(
        Skill.load(SKILL_PATH),
        _bundle(),
        _allowlist(_root(tmp_path)),
        runner=ClaudeAgentSdkRunner(),
        budget=_budget(),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts",
    )
    assert result.status == "SKIPPED"
    assert result.stop_reason == "precondition"


@pytest.mark.parametrize(
    "command",
    ["echo ok && rm -rf /", "echo ok | tee out", "echo ok; ls", "echo $(whoami)", "echo `id`"],
)
def test_sdk_bash_with_shell_operators_maps_to_a_refusal(command):
    call, reason = sdk_call_to_tool_call("Bash", {"command": command})
    assert call is None
    assert "operator" in reason or "one plain argv" in reason


def test_sdk_bash_maps_git_gh_and_pytest_to_their_own_tools():
    assert isinstance(sdk_call_to_tool_call("Bash", {"command": "git status"})[0], Git)
    assert isinstance(sdk_call_to_tool_call("Bash", {"command": "gh pr create -f"})[0], Gh)
    assert isinstance(sdk_call_to_tool_call("Bash", {"command": "uv run pytest -q"})[0], Pytest)
    assert isinstance(sdk_call_to_tool_call("Bash", {"command": "echo ok"})[0], Shell)
    call, reason = sdk_call_to_tool_call("Bash", {"command": "gh pr merge 1"})
    assert call is None and "pr merge" in reason
    call, reason = sdk_call_to_tool_call("Task", {"prompt": "x"})
    assert call is None and "not in the allowlist" in reason


# ------------------------------------------------ the gate, tool by tool


def test_gh_pr_merge_cannot_be_expressed_as_a_turn():
    with pytest.raises(ValidationError, match="pr merge"):
        Turn.model_validate({"call": {"tool": "gh", "args": ["pr", "merge", "1"]}})
    with pytest.raises(ValidationError):
        GhTool(root=Path("."), subcommands=["pr merge"])
    Turn.model_validate({"call": {"tool": "gh", "args": ["pr", "create", "--fill"]}})


def test_git_merge_rebase_reset_and_stash_are_not_subcommands(tmp_path):
    for banned in ("merge", "rebase", "reset", "stash"):
        with pytest.raises(ValidationError):
            GitTool(root=tmp_path, subcommands=[banned])
    root = _git_repo(tmp_path)
    gate = _gate(
        ToolAllowlist(git=GitTool(root=root, subcommands=["status", "push", "switch"])), tmp_path
    )
    for banned in ("merge", "rebase", "reset", "stash"):
        verdict = gate.check(Git(args=[banned]))
        assert not verdict.allowed and banned in verdict.reason


def test_git_push_only_to_the_current_branch_with_u_origin(tmp_path):
    root = _git_repo(tmp_path)
    gate = _gate(ToolAllowlist(git=GitTool(root=root, subcommands=["push", "switch"])), tmp_path)
    assert gate.check(Git(args=["push", "-u", "origin", "lane/x"])).allowed
    for argv in (
        ["push"],
        ["push", "origin", "main"],
        ["push", "-u", "origin", "main"],
        ["push", "-u", "origin", "other"],
        ["push", "--force", "-u", "origin", "lane/x"],
        ["push", "-f", "-u", "origin", "lane/x"],
        ["push", "--force-with-lease", "-u", "origin", "lane/x"],
        ["-C", "/", "push", "-u", "origin", "lane/x"],
    ):
        verdict = gate.check(Git(args=argv))
        assert not verdict.allowed, argv
    assert not gate.check(Git(args=["switch", "main"])).allowed
    assert gate.check(Git(args=["switch", "-c", "lane/y"])).allowed


def test_file_tools_stay_inside_the_root_and_never_write_git(tmp_path):
    root = _root(tmp_path)
    (root / ".git").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (root / "link").symlink_to(outside)
    gate = _gate(ToolAllowlist(files=FileTools(root=root)), tmp_path)
    assert gate.check(ReadFile(path="README.md")).allowed
    assert gate.check(ReadFile(path=str(root / "README.md"))).allowed
    assert not gate.check(ReadFile(path="../outside.txt")).allowed
    assert not gate.check(ReadFile(path=str(outside))).allowed
    assert not gate.check(ReadFile(path="link")).allowed  # symlinks are followed
    assert gate.check(WriteFile(path="new/file.txt", content="x")).allowed
    assert not gate.check(WriteFile(path=".git/config", content="x")).allowed
    assert not gate.check(WriteFile(path="../escape.txt", content="x")).allowed
    read_only = _gate(ToolAllowlist(files=FileTools(root=root, write=False)), tmp_path)
    assert not read_only.check(WriteFile(path="new/file.txt", content="x")).allowed
    assert read_only.check(ReadFile(path="README.md")).allowed


def test_shell_refuses_undeclared_prefixes_and_operators(tmp_path):
    root = _root(tmp_path)
    gate = _gate(
        ToolAllowlist(shell=ShellTool(root=root, argv_prefixes=[["echo"], ["uv", "run", "ruff"]])),
        tmp_path,
    )
    assert gate.check(Shell(argv=["echo", "hi"])).allowed
    assert gate.check(Shell(argv=["uv", "run", "ruff", "check", "."])).allowed
    assert not gate.check(Shell(argv=["uv", "run", "python", "-c", "1"])).allowed
    assert not gate.check(Shell(argv=["rm", "-rf", "."])).allowed
    for bad in (["echo", "a", "&&", "rm"], ["echo", "a", "|", "cat"], ["echo", "$(id)"]):
        assert not gate.check(Shell(argv=bad)).allowed, bad


def test_pytest_args_are_limited_to_paths_inside_root_and_the_named_flags(tmp_path):
    root = _root(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    gate = _gate(ToolAllowlist(pytest=PytestTool(root=root)), tmp_path)
    assert gate.check(Pytest(args=[])).allowed
    assert gate.check(Pytest(args=["-q", "-x", "tests/test_x.py", "-k", "x"])).allowed
    assert gate.check(Pytest(args=["-p", "no:cacheprovider"])).allowed
    assert not gate.check(Pytest(args=["../other"])).allowed
    assert not gate.check(Pytest(args=["-p", "evil_plugin"])).allowed
    assert not gate.check(Pytest(args=["--co"])).allowed


def test_the_shell_subprocess_env_is_reduced_to_path_home_and_the_stage_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_CANARY", "leaks")
    monkeypatch.setenv("BUILD_LANE_STAGE", str(tmp_path))
    monkeypatch.delenv("BUILD_LANE_ISSUE", raising=False)
    env = reduced_env(["BUILD_LANE_STAGE", "BUILD_LANE_ISSUE"])
    assert set(env) == {"PATH", "HOME", "BUILD_LANE_STAGE"}

    root = _root(tmp_path)
    gate = _gate(
        ToolAllowlist(
            shell=ShellTool(root=root, argv_prefixes=[[sys.executable, "-c"]]),
            env_passthrough=["BUILD_LANE_STAGE"],
        ),
        tmp_path,
    )
    result = gate.call(
        Shell(
            argv=[sys.executable, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"]
        ),
        turn=1,
    )
    assert result.ok and result.exit_code == 0
    names = set(json.loads(result.stdout))
    assert "RUNNER_CANARY" not in names
    # The child interpreter adds LC_CTYPE itself (PEP 538 locale coercion)
    # and CoreFoundation adds __CF_USER_TEXT_ENCODING on macOS; neither
    # came through the gate. Everything else is exactly the reduced set.
    assert names - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {"PATH", "HOME", "BUILD_LANE_STAGE"}


# ------------------------------------------------------------ eval_first


def test_eval_first_refuses_gh_pr_create_until_a_test_file_and_a_passing_pytest_exist(
    tmp_path, monkeypatch
):
    root = _git_repo(tmp_path)
    pytest_exit = {"code": 0}

    from core.llm import tools

    original = tools.run_subprocess

    def fake_run(argv, **kw):
        if argv[:3] == ["uv", "run", "pytest"]:
            return tools.CompletedRun(pytest_exit["code"], "", "", 0.0)
        return original(argv, **kw)

    monkeypatch.setattr(tools, "run_subprocess", fake_run)
    gate = _gate(
        ToolAllowlist(
            files=FileTools(root=root),
            gh=GhTool(root=root, subcommands=["pr create"], gates=["eval_first"]),
            pytest=PytestTool(root=root),
        ),
        tmp_path,
    )
    create = Gh(args=["pr", "create", "--fill"])

    # (a) no new or changed test file.
    verdict = gate.check(create)
    assert not verdict.allowed and "test file" in verdict.reason

    # A test file appears (untracked counts), but no pytest has run since.
    assert gate.call(
        WriteFile(path="tests/test_new.py", content="def test_a():\n    pass\n"), turn=1
    ).ok
    verdict = gate.check(create)
    assert not verdict.allowed and "pytest" in verdict.reason

    # A failing pytest does not open the gate.
    pytest_exit["code"] = 1
    assert gate.call(Pytest(args=[]), turn=2).exit_code == 1
    verdict = gate.check(create)
    assert not verdict.allowed and "pytest" in verdict.reason

    # A passing pytest after the last write does.
    pytest_exit["code"] = 0
    assert gate.call(Pytest(args=[]), turn=3).exit_code == 0
    assert gate.check(create).allowed

    # Another write after that pytest closes it again.
    assert gate.call(WriteFile(path="src.py", content="x = 2\n"), turn=4).ok
    verdict = gate.check(create)
    assert not verdict.allowed and "pytest" in verdict.reason

    # Without the gate declared, pr create needs no eval (row 7.18 decides per lane).
    ungated = _gate(ToolAllowlist(gh=GhTool(root=root, subcommands=["pr create"])), tmp_path)
    assert ungated.check(create).allowed
    assert not ungated.check(Gh(args=["issue", "edit", "1"])).allowed


# --------------------------------------------------------------- budgets


def _pricing_1usd_per_token() -> Pricing:
    return Pricing(input_usd_per_mtok=Decimal(1_000_000), output_usd_per_mtok=Decimal(1_000_000))


def test_max_turns_stops_the_run_failed_with_the_last_progress(tmp_path):
    result = _run(tmp_path, FixtureAdapter(_turn_replies()), budget=_budget(max_turns=2))
    assert result.status == "FAILED"
    assert result.stop_reason == "turns"
    assert result.note == "ran echo"
    assert result.turns == 2


def test_max_seconds_stops_the_run_failed_with_the_last_progress(tmp_path):
    ticks = iter(range(60, 60 * 100, 60))
    result = _run(
        tmp_path,
        FixtureAdapter(_turn_replies()),
        budget=_budget(max_seconds=100),
        clock=lambda: float(next(ticks)),
    )
    assert result.status == "FAILED"
    assert result.stop_reason == "seconds"
    assert result.note == "wrote hello.txt"
    assert result.turns == 1


def test_max_usd_stops_the_run_failed_with_the_last_progress(tmp_path):
    adapter = FixtureAdapter(_turn_replies(), usage=Usage(100, 50))
    result = _run(
        tmp_path,
        adapter,
        budget=_budget(max_usd=Decimal("200")),
        pricing=_pricing_1usd_per_token(),
    )
    assert result.status == "FAILED"
    assert result.stop_reason == "usd"
    assert result.note == "ran echo"
    assert result.usd == Decimal("300")
    assert result.turns == 2


def test_max_tokens_stops_the_run_failed_with_the_last_progress(tmp_path):
    adapter = FixtureAdapter(_turn_replies(), usage=Usage(100, 50))
    result = _run(tmp_path, adapter, budget=_budget(max_tokens=200))
    assert result.status == "FAILED"
    assert result.stop_reason == "tokens"
    assert result.note == "ran echo"
    assert result.input_tokens + result.output_tokens == 300


def test_max_usd_with_no_pricing_is_a_skipped_precondition_before_any_call(tmp_path):
    adapter = FixtureAdapter(_turn_replies())
    result = _run(tmp_path, adapter, budget=_budget(max_usd=Decimal("1")))
    assert result.status == "SKIPPED"
    assert result.stop_reason == "precondition"
    assert adapter.calls == []
    assert result.transcript_path.exists()


# ------------------------------------------------------------ redaction


PLANTED_KEY = "sk-plantedsecretkey1234567890"
PLANTED_TOKEN = "ghp_plantedtoken1234567890abcdef"
PLANTED_EIN = "98-7654321"
PLANTED_SSN = "123-45-6789"
PLANTED_ENV_VALUE = "hunter2-planted-provider-secret"


def test_the_transcript_and_the_result_carry_no_key_tin_or_provider_env_value(
    tmp_path, monkeypatch
):
    """The W-9 lane's planted-TIN pattern
    (test_no_tin_shaped_content_ever_leaves_the_document), applied to the
    session: plant one of each shape in a model reply, a written file, and
    the context bundle, then assert absence everywhere the runner writes."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", PLANTED_ENV_VALUE)
    root = _root(tmp_path)
    turns = [
        {
            "reason": f"using {PLANTED_KEY} and Bearer {PLANTED_TOKEN}",
            "progress": f"EIN {PLANTED_EIN} noted",
            "call": {
                "tool": "write_file",
                "path": "vendor.txt",
                "content": f"TIN {PLANTED_SSN} key {PLANTED_ENV_VALUE}\n",
            },
        },
        {"reason": "read it back", "call": {"tool": "read_file", "path": "vendor.txt"}},
        {
            "reason": "done",
            "note": f"# Session note\n\nfiled EIN {PLANTED_EIN} with {PLANTED_KEY}\n",
        },
    ]
    bundle = ContextBundle(items=[ContextItem.from_text("form", f"W-9 EIN {PLANTED_EIN}")])
    result = run(
        Skill.load(SKILL_PATH),
        bundle,
        _allowlist(root),
        runner=GatewayLoopRunner(FixtureAdapter(_turn_replies(turns))),
        budget=_budget(),
        model="policy-chosen-model",
        transcript_dir=tmp_path / "transcripts",
        redact_env=["TEST_PROVIDER_KEY"],
    )
    assert result.status == "FINISHED"
    transcript = result.transcript_path.read_text(encoding="utf-8")
    dumped = result.model_dump_json()
    for planted in (PLANTED_KEY, PLANTED_TOKEN, PLANTED_EIN, PLANTED_SSN, PLANTED_ENV_VALUE):
        assert planted not in transcript, planted
        assert planted not in dumped, planted
    assert "<redacted:tin>" in transcript
    assert "<redacted:key>" in transcript
    assert "<redacted:TEST_PROVIDER_KEY>" in transcript
    assert "<redacted:tin>" in result.note
    # The file on disk is the model's business, untouched by redaction.
    assert PLANTED_SSN in (root / "vendor.txt").read_text()


def test_redact_is_recursive_and_leaves_ordinary_text_alone():
    payload = {"a": [f"x {PLANTED_EIN}", {"b": "Bearer abcdefghijklmnop"}], "n": 3}
    out = redact(payload, env_values=[])
    assert out == {"a": ["x <redacted:tin>", {"b": "<redacted:key>"}], "n": 3}
    assert redact("invoice 2026-09-12 total 1875.00", env_values=[]) == (
        "invoice 2026-09-12 total 1875.00"
    )


# ------------------------------------------------------------ the digest


def test_same_inputs_same_digest_and_a_changed_allowlist_changes_it(tmp_path):
    root = _root(tmp_path)
    skill = Skill.load(SKILL_PATH)
    a = inputs_digest(skill, _bundle(), _allowlist(root), "gateway_loop", "m")
    b = inputs_digest(skill, _bundle(), _allowlist(root), "gateway_loop", "m")
    assert a == b and len(a) == 16
    wider = _allowlist(root, git=GitTool(root=root, subcommands=["status"]))
    assert inputs_digest(skill, _bundle(), wider, "gateway_loop", "m") != a
    assert inputs_digest(skill, _bundle(), _allowlist(root), "claude_agent_sdk", "m") != a
    assert inputs_digest(skill, _bundle(), _allowlist(root), "gateway_loop", "other") != a
    reordered = ContextBundle(items=list(reversed(_bundle().items)))
    assert inputs_digest(skill, reordered, _allowlist(root), "gateway_loop", "m") != a


def test_context_bundle_digest_and_render_cover_file_items(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nall quiet\n", encoding="utf-8")
    bundle = ContextBundle(items=[ContextItem.from_file("report", report)])
    assert bundle.items[0].kind == "file"
    assert len(bundle.items[0].sha256) == 64
    rendered = bundle.render()
    assert "## report" in rendered and "all quiet" in rendered
    report.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report"):
        bundle.render()
