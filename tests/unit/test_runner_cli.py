"""``engine runner run`` (phase 7 row 7.17, part A).

The runner (row 7.16) shipped with no entry point: nothing outside a test
could hand it a skill, a bundle, and an allowlist. This is that entry point,
and these evals pin what it promises:

- the context bundle is built from the params IN THE ORDER GIVEN, because
  order is part of the runner's contract (``ContextBundle.digest``);
- a ``dir:`` item is bounded and deterministic: the last ``dir_max`` ``*.md``
  files by name, so a stage that grows for a year still makes the same
  bundle shape;
- the allowlist is data from the caller, and ``gh`` ALWAYS carries the
  ``eval_first`` gate: the CLI has no knob that opens a PR lane without it;
- which runner holds the conversation comes from the tenant's policy (the
  tier's adapter), so a host on API keys gets the loop runner without
  changing a command line; ``--adapter`` overrides it;
- the exit codes the wrappers read: 0 FINISHED, 75 SKIPPED, 1 FAILED,
  2 a usage or configuration error;
- the note file is written on FINISHED and NEVER on FAILED (a wrapper
  installs what it finds; a partial note that looks finished is the one
  thing the triage lane must not produce).

No network and no key: the replay adapter drives the loop runner from
recorded turns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.engine.cli import build_parser, main
from core.llm import runner_cli
from core.llm.runner_cli import LaneError

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"
SKILL = FIXTURES / "skills" / "edit-and-run" / "SKILL.md"
TURNS = FIXTURES / "turns" / "note-only.json"


def _params(**kw: str) -> dict[str, str]:
    return dict(kw)


# ---------------------------------------------------------------- the parser


def test_the_parser_carries_runner_run():
    args = build_parser().parse_args(
        ["runner", "run", "demo", str(SKILL), "--adapter", "loop", "--param", "root=.", "--json"]
    )
    assert args.command == "runner"
    assert args.runner_command == "run"
    assert args.tenant == "demo"
    assert args.skill == str(SKILL)
    assert args.adapter == "loop"
    assert args.param == ["root=."]
    assert args.json is True


# ------------------------------------------------------------- the bundle


def test_context_items_keep_the_order_the_params_gave_them(tmp_path):
    one = tmp_path / "one.md"
    one.write_text("first file", encoding="utf-8")
    bundle = runner_cli.build_context(
        runner_cli.build_spec([f"file:alpha={one}", "text:beta=inline text", f"file:gamma={one}"])
    )
    assert [item.name for item in bundle.items] == ["alpha", "beta", "gamma"]
    assert bundle.items[1].content() == "inline text"
    assert bundle.items[0].content() == "first file"


def test_a_dir_item_takes_the_last_markdown_files_by_name(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    for day in range(1, 6):
        (stage / f"audit-2026-09-0{day}.md").write_text(f"day {day}", encoding="utf-8")
    (stage / "ignored.txt").write_text("not markdown", encoding="utf-8")
    bundle = runner_cli.build_context(runner_cli.build_spec([f"dir:reports={stage}", "dir_max=2"]))
    assert [item.name for item in bundle.items] == [
        "reports/audit-2026-09-04.md",
        "reports/audit-2026-09-05.md",
    ]


def test_a_dir_item_can_name_a_glob_and_the_stage_trap_it_was_written_for(tmp_path):
    """The live triage stage holds the day's note beside the audit reports
    (`triage-2026-09-17.note.md` next to `audit-2026-09-17.md`), and `triage`
    sorts after `audit`, so a plain `*.md` directory item would hand the
    session three of its own old notes and NOT tonight's report. A `dir:`
    value carrying a glob names what to take."""
    stage = tmp_path / "stage"
    stage.mkdir()
    for day in (15, 16, 17):
        (stage / f"audit-2026-09-{day}.md").write_text(f"report {day}", encoding="utf-8")
        (stage / f"triage-2026-09-{day}.note.md").write_text(f"note {day}", encoding="utf-8")
    spec = runner_cli.build_spec([f"dir:reports={stage}/audit-*.md", "dir_max=2"])
    bundle = runner_cli.build_context(spec)
    assert [item.name for item in bundle.items] == [
        "reports/audit-2026-09-16.md",
        "reports/audit-2026-09-17.md",
    ]


def test_a_dir_item_that_is_empty_or_absent_contributes_nothing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    spec = runner_cli.build_spec([f"dir:reports={empty}", f"dir:gone={tmp_path / 'nope'}"])
    bundle = runner_cli.build_context(spec)
    assert bundle.items == []


def test_a_missing_file_item_is_a_usage_error(tmp_path):
    with pytest.raises(LaneError) as exc:
        runner_cli.build_context(runner_cli.build_spec([f"file:today={tmp_path / 'absent.md'}"]))
    assert "absent.md" in str(exc.value)


def test_an_unknown_param_is_a_usage_error():
    with pytest.raises(LaneError) as exc:
        runner_cli.build_spec(["wat=1"])
    assert "wat" in str(exc.value)


# ------------------------------------------------------------ the allowlist


def test_tools_read_is_read_only_and_write_opens_it(tmp_path):
    read = runner_cli.build_allowlist(runner_cli.build_spec([f"root={tmp_path}", "tools=read"]))
    assert read.files.write is False
    assert read.git is None and read.gh is None and read.pytest is None and read.shell is None
    write = runner_cli.build_allowlist(runner_cli.build_spec([f"root={tmp_path}", "tools=write"]))
    assert write.files.write is True


def test_gh_always_carries_the_eval_first_gate(tmp_path):
    allowlist = runner_cli.build_allowlist(
        runner_cli.build_spec([f"root={tmp_path}", "tools=write,git,gh,pytest"])
    )
    assert allowlist.gh.gates == ["eval_first"]
    assert allowlist.gh.subcommands == ["pr create", "issue edit", "pr comment"]
    assert "merge" not in " ".join(allowlist.gh.subcommands)
    assert allowlist.git.root == tmp_path
    assert allowlist.pytest.root == tmp_path


def test_the_proposal_gate_is_opt_in_and_rides_beside_eval_first(tmp_path):
    """Row 7.18: a lane asks for the proposal gate; it never replaces
    eval_first, so a diff that touches source still needs a green suite."""
    plain = runner_cli.build_allowlist(runner_cli.build_spec([f"root={tmp_path}", "tools=gh"]))
    assert plain.gh.gates == ["eval_first"]
    proposal = runner_cli.build_allowlist(
        runner_cli.build_spec([f"root={tmp_path}", "tools=gh", "proposal=1"])
    )
    assert proposal.gh.gates == ["eval_first", "proposal"]
    off = runner_cli.build_allowlist(
        runner_cli.build_spec([f"root={tmp_path}", "tools=gh", "proposal=0"])
    )
    assert off.gh.gates == ["eval_first"]


def test_shell_prefixes_are_data_from_the_caller(tmp_path):
    allowlist = runner_cli.build_allowlist(
        runner_cli.build_spec([f"root={tmp_path}", "shell=uv run ruff;uv run python -m"])
    )
    assert allowlist.shell.argv_prefixes == [
        ["uv", "run", "ruff"],
        ["uv", "run", "python", "-m"],
    ]


def test_an_unknown_tool_name_is_a_usage_error(tmp_path):
    with pytest.raises(LaneError) as exc:
        runner_cli.build_allowlist(runner_cli.build_spec([f"root={tmp_path}", "tools=curl"]))
    assert "curl" in str(exc.value)


# --------------------------------------------------------------- the budget


def test_the_budget_has_defaults_and_every_stop_is_settable():
    default = runner_cli.build_budget(runner_cli.build_spec([]))
    assert default.max_turns == runner_cli.DEFAULT_MAX_TURNS
    assert default.max_seconds == runner_cli.DEFAULT_MAX_SECONDS
    assert default.max_usd is None and default.max_tokens is None
    named = runner_cli.build_budget(
        runner_cli.build_spec(["max_turns=7", "max_seconds=60", "max_usd=1.50", "max_tokens=1000"])
    )
    assert (named.max_turns, named.max_seconds) == (7, 60)
    assert str(named.max_usd) == "1.50"
    assert named.max_tokens == 1000


def test_the_job_type_defaults_to_the_skill_name_and_the_param_overrides_it():
    spec = runner_cli.build_spec([])
    assert runner_cli.job_type_for(SKILL, spec) == "edit_and_run"
    assert runner_cli.job_type_for(SKILL, runner_cli.build_spec(["job=audit_triage"])) == (
        "audit_triage"
    )


# ---------------------------------------------------------------- end to end


def _run(tmp_path, *params: str, adapter: str = "replay", json_out: bool = False) -> int:
    argv = ["runner", "run", "demo", str(SKILL), "--adapter", adapter]
    # The fixture skill is not a policy job; `job=` is how a lane says which
    # tier pays for it (the demo tenant serves audit_triage from its fixture).
    for param in (
        f"root={tmp_path}",
        f"transcripts={tmp_path / 'transcripts'}",
        "job=audit_triage",
        *params,
    ):
        argv += ["--param", param]
    if json_out:
        argv.append("--json")
    return main(argv)


def test_a_finished_run_writes_the_note_and_exits_zero(tmp_path, capsys):
    note = tmp_path / "note.md"
    code = _run(tmp_path, f"replay={TURNS}", f"note={note}", "text:issue=a quiet night")
    out = capsys.readouterr().out
    assert code == 0, out
    assert note.read_text(encoding="utf-8").startswith("# Session note")
    assert "FINISHED" in out
    transcripts = sorted((tmp_path / "transcripts").glob("*.jsonl"))
    assert len(transcripts) == 1, transcripts


def test_a_failed_run_leaves_no_note_behind(tmp_path, capsys):
    """A budget stop is FAILED, and the wrapper's rule is that a missing note
    is a failed run: the CLI must not hand it a half-finished one."""
    note = tmp_path / "note.md"
    code = _run(tmp_path, f"replay={TURNS}", f"note={note}", "max_turns=0")
    out = capsys.readouterr().out
    assert code == 1, out
    assert not note.exists()
    assert "FAILED" in out


def test_a_skipped_run_exits_75(tmp_path, capsys, monkeypatch):
    """The SDK runner without the SDK is SKIPPED precondition, and 75 is what
    the wrappers already read as "did not run, nothing is wrong"."""

    def no_sdk():
        raise ModuleNotFoundError("claude_agent_sdk")

    monkeypatch.setattr("core.llm.adapters.claude_sdk.sdk", no_sdk)
    code = _run(tmp_path, "note=" + str(tmp_path / "note.md"), adapter="sdk")
    assert code == 75, capsys.readouterr().out


def test_json_prints_the_whole_result_object(tmp_path, capsys):
    code = _run(tmp_path, f"replay={TURNS}", json_out=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    for field in (
        "status",
        "stop_reason",
        "note",
        "files_changed",
        "commands_run",
        "refused",
        "input_tokens",
        "output_tokens",
        "usd",
        "turns",
        "wall_seconds",
        "model",
        "runner",
        "inputs_digest",
    ):
        assert field in payload, field
    assert payload["status"] == "FINISHED"
    assert payload["runner"] == "gateway_loop"


def test_an_unknown_tenant_is_exit_2(tmp_path, capsys):
    code = main(["runner", "run", "no-such-tenant", str(SKILL), "--param", f"root={tmp_path}"])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_a_skill_file_that_is_not_a_skill_is_exit_2(tmp_path, capsys):
    plain = tmp_path / "PLAIN.md"
    plain.write_text("no frontmatter here", encoding="utf-8")
    code = main(["runner", "run", "demo", str(plain), "--param", f"root={tmp_path}"])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_the_runner_comes_from_the_policy_unless_the_flag_overrides_it():
    """The tenant's tier names the adapter; the seat gets the SDK runner and
    every other adapter gets the loop. A product host on API keys changes no
    command line."""
    assert runner_cli.runner_kind_for("claude_agent_sdk", None) == "sdk"
    assert runner_cli.runner_kind_for("openai_compat", None) == "loop"
    assert runner_cli.runner_kind_for("fixture", None) == "loop"
    assert runner_cli.runner_kind_for("claude_agent_sdk", "loop") == "loop"


def test_the_transcript_redacts_the_tiers_key_variable(tmp_path, monkeypatch, capsys):
    """The tenant names the variable; its VALUE must never reach a transcript
    (docs/runner-design.md, "Transcripts")."""
    monkeypatch.setenv("DEMO_MODEL_KEY", "sk-not-a-real-key-abcdef")
    code = _run(tmp_path, f"replay={TURNS}", "text:hint=the key is sk-not-a-real-key-abcdef")
    assert code == 0, capsys.readouterr().out
    written = (sorted((tmp_path / "transcripts").glob("*.jsonl"))[0]).read_text(encoding="utf-8")
    assert "sk-not-a-real-key-abcdef" not in written
