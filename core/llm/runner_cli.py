"""``engine runner run <tenant> <skill> [--adapter ...] [--param K=V]``: the
runner's entry point (phase 7 row 7.17, part A; ``docs/runner-design.md``).

Row 7.16 landed :func:`core.llm.runner.run` with no caller. This module is
the smallest surface that hands it the three things it takes -- a skill
file, a deterministic context bundle, and a tool allowlist -- and prints the
result object. Everything the caller decides arrives as ``--param K=V``, so
a lane is a command line a wrapper can hold and a test can pin:

======================  ====================================================
``root=<dir>``          where every tool works (default: the current dir)
``tools=<names>``       ``read`` | ``write`` | ``git`` | ``gh`` | ``pytest``
``shell=<prefixes>``    ``;``-separated argv prefixes, each space-split
``base_ref=<ref>``      what the gh gates diff against (default origin/main)
``proposal=1``          also carry the ``proposal`` gate (row 7.18): a design
                        note plus one failing eval, never a source change
``text:<name>=<text>``  one context item, inline
``file:<name>=<path>``  one context item, a file
``dir:<name>=<dir>``    the last ``dir_max`` ``*.md`` files in that directory,
                        or in ``<dir>/<glob>`` form the last ``dir_max`` files
                        the glob names
``dir_max=<n>``         how many (default 3)
``note=<path>``         where a FINISHED note is written; never a FAILED one
``transcripts=<dir>``   where the JSONL transcript lands
``max_turns`` ...       the four budget stops (``max_seconds``, ``max_usd``,
                        ``max_tokens``)
``job=<job_type>``      the ``[llm.jobs]`` key (default: the skill's own name
                        with hyphens as underscores)
``replay=<path>``       recorded ``Turn`` replies, with ``--adapter replay``
======================  ====================================================

Three rules this module owns, none of which any caller can switch off:

- **``gh`` always carries the ``eval_first`` gate.** Row 7.17 moves the
  eval-first rule out of skill prose and into Python: a lane that can open a
  PR cannot be configured to open one without a new or changed test file and
  a passing suite. There is no parameter for it. ``proposal=1`` ADDS the
  narrower proposal gate beside it (row 7.18) and can never take its place:
  a diff that touches source is not proposal-shaped, so it still answers to
  ``eval_first``.
- **The note file is written on FINISHED and never on FAILED.** The headless
  wrappers install whatever note they find and treat a missing one as a
  failed run; a half-finished note that looks finished is the one thing that
  must not reach a morning reader. The progress text still goes to stdout
  and to the transcript.
- **Which runner holds the conversation comes from the tenant policy**, not
  from the command line: the tier's adapter names it (the Claude Agent SDK
  tier gets the SDK runner, every other adapter gets the in-repo loop), so a
  host on API keys runs the same lane with no edit. ``--adapter`` overrides
  it for a hand run or a replay.

Exit codes are what the wrappers already read: 0 FINISHED, 75 SKIPPED (a
precondition, nothing is wrong), 1 FAILED, 2 a usage or configuration error.

No model id and no tenant name live here; both are data the policy resolves.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from core.engine.config import load_tenant
from core.llm.policy import ResolvedModel, build_adapter, resolve
from core.llm.runner import (
    Budget,
    ContextBundle,
    ContextItem,
    GatewayLoopRunner,
    Runner,
    RunnerResult,
    Skill,
    run,
)
from core.llm.tools import (
    GH_SUBCOMMANDS,
    GIT_SUBCOMMANDS,
    FileTools,
    GhTool,
    GitTool,
    PytestTool,
    ShellTool,
    ToolAllowlist,
)

DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_SECONDS = 3600
DEFAULT_DIR_MAX = 3
DIR_SUFFIX = ".md"
TRANSCRIPT_DIRNAME = ".runner-transcripts"
DATA_ROOT_ENV = "ENGINE_DATA_ROOT"
"""The container's one volume (row 7.21); transcripts live under it when it
is set, the way the seat's scratch directories do."""

TOOL_NAMES = ("read", "write", "git", "gh", "pytest")
ADAPTER_CHOICES = ("sdk", "loop", "replay")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 75

_SIMPLE_KEYS = (
    "root",
    "tools",
    "shell",
    "base_ref",
    "dir_max",
    "note",
    "transcripts",
    "max_turns",
    "max_seconds",
    "max_usd",
    "max_tokens",
    "job",
    "replay",
    "proposal",
)
_ITEM_PREFIXES = ("text", "file", "dir")


class LaneError(ValueError):
    """A usage error in the params: exit 2, nothing ran."""


@dataclass(frozen=True)
class LaneSpec:
    """The parsed params. ``items`` keeps the order the command line gave
    them, because order is part of the bundle's contract."""

    values: dict[str, str] = field(default_factory=dict)
    items: tuple[tuple[str, str, str], ...] = ()
    """(kind, name, value) per context item, in order."""

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def path(self, key: str) -> Path | None:
        value = self.values.get(key)
        return Path(value).expanduser() if value else None

    def flag(self, key: str) -> bool:
        """A switch param. Anything but 1/true/yes/on is off."""
        return self.values.get(key, "").strip().lower() in ("1", "true", "yes", "on")

    def number(self, key: str, default: int | None = None) -> int | None:
        raw = self.values.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise LaneError(f"--param {key}={raw!r} expects a whole number") from exc


def build_spec(params: list[str]) -> LaneSpec:
    """Parse ``K=V`` params into a :class:`LaneSpec`. An unknown key is an
    error: a typo must never silently drop a context item or a budget."""
    values: dict[str, str] = {}
    items: list[tuple[str, str, str]] = []
    for pair in params:
        if "=" not in pair:
            raise LaneError(f"--param expects K=V, got {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if ":" in key:
            prefix, _, name = key.partition(":")
            if prefix not in _ITEM_PREFIXES or not name:
                raise LaneError(
                    f"--param {key!r}: context items are "
                    f"{', '.join(p + ':<name>' for p in _ITEM_PREFIXES)}"
                )
            items.append((prefix, name, value))
            continue
        if key not in _SIMPLE_KEYS:
            raise LaneError(
                f"--param {key!r} is not a runner parameter "
                f"({', '.join(_SIMPLE_KEYS)}, or a context item)"
            )
        values[key] = value
    return LaneSpec(values=values, items=tuple(items))


# ---- the three inputs ---------------------------------------------------------


def build_context(spec: LaneSpec) -> ContextBundle:
    """The bundle, in the order the params gave. A ``dir:`` item is bounded
    and deterministic on purpose: the last ``dir_max`` ``*.md`` files by
    name, so a stage that collects a year of dated files still makes a
    bundle of the same shape, and the digest only moves when the newest
    files do."""
    limit = spec.number("dir_max", DEFAULT_DIR_MAX) or DEFAULT_DIR_MAX
    items: list[ContextItem] = []
    for kind, name, value in spec.items:
        if kind == "text":
            items.append(ContextItem.from_text(name, value))
            continue
        if kind == "file":
            path = Path(value).expanduser()
            if not path.is_file():
                raise LaneError(f"--param file:{name}={value}: no such file")
            items.append(ContextItem.from_file(name, path))
            continue
        base, pattern = _dir_target(value)
        if not base.is_dir():
            continue
        found = sorted(p for p in base.glob(pattern) if p.is_file())
        for path in found[-limit:]:
            items.append(ContextItem.from_file(f"{name}/{path.name}", path))
    return ContextBundle(items=items)


def _dir_target(value: str) -> tuple[Path, str]:
    """A ``dir:`` value is a directory, or a glob naming what to take from
    one. The glob form exists because a real stage holds more than the files
    a lane wants: the triage stage keeps the day's note (``triage-*.note.md``)
    beside the audit reports, and ``triage`` sorts after ``audit``, so a plain
    ``*.md`` would hand a session three of its own old notes and leave
    tonight's report out."""
    path = Path(value).expanduser()
    if any(ch in path.name for ch in "*?["):
        return path.parent, path.name
    return path, f"*{DIR_SUFFIX}"


def build_allowlist(spec: LaneSpec) -> ToolAllowlist:
    """The tools the session gets, from ``tools=`` and ``shell=``. ``gh``
    always carries ``eval_first``; the CLI has no knob that opens a PR lane
    without it (row 7.17). ``proposal=1`` adds the proposal gate beside it
    (row 7.18), which is narrower, never a way around it."""
    root = spec.path("root") or Path.cwd()
    root = root.resolve()
    names = [n.strip() for n in spec.get("tools").split(",") if n.strip()]
    unknown = [n for n in names if n not in TOOL_NAMES]
    if unknown:
        raise LaneError(
            f"--param tools: {', '.join(unknown)} is not a runner tool ({', '.join(TOOL_NAMES)})"
        )
    prefixes = [
        part.split() for part in (p.strip() for p in spec.get("shell").split(";")) if part.strip()
    ]
    files = None
    if "read" in names or "write" in names:
        files = FileTools(root=root, write="write" in names)
    return ToolAllowlist(
        files=files,
        shell=ShellTool(root=root, argv_prefixes=prefixes) if prefixes else None,
        git=GitTool(root=root, subcommands=list(GIT_SUBCOMMANDS)) if "git" in names else None,
        gh=(
            GhTool(
                root=root,
                subcommands=list(GH_SUBCOMMANDS),
                gates=["eval_first", "proposal"] if spec.flag("proposal") else ["eval_first"],
                base_ref=spec.get("base_ref") or "origin/main",
            )
            if "gh" in names
            else None
        ),
        pytest=PytestTool(root=root) if "pytest" in names else None,
    )


def build_budget(spec: LaneSpec) -> Budget:
    raw_usd = spec.get("max_usd")
    try:
        max_usd = Decimal(raw_usd) if raw_usd else None
    except InvalidOperation as exc:
        raise LaneError(f"--param max_usd={raw_usd!r} expects a number") from exc
    return Budget(
        max_turns=spec.number("max_turns", DEFAULT_MAX_TURNS),
        max_seconds=spec.number("max_seconds", DEFAULT_MAX_SECONDS),
        max_usd=max_usd,
        max_tokens=spec.number("max_tokens"),
    )


def job_type_for(skill_path: Path, spec: LaneSpec) -> str:
    """The ``[llm.jobs]`` key: the param when given, else the skill's own
    folder name with hyphens as underscores, so a skill and the policy row
    that pays for it are named the same thing."""
    named = spec.get("job")
    if named:
        return named
    return Path(skill_path).parent.name.replace("-", "_")


def transcript_dir(spec: LaneSpec) -> Path:
    """An explicit value wins, then the container's data volume, then a
    directory beside the run (the engine's own state convention)."""
    named = spec.path("transcripts")
    if named:
        return named
    data_root = os.environ.get(DATA_ROOT_ENV)
    if data_root:
        return Path(data_root).expanduser() / "runner-transcripts"
    return Path.cwd() / TRANSCRIPT_DIRNAME


# ---- the runner ---------------------------------------------------------------


def runner_kind_for(adapter_name: str, override: str | None) -> str:
    """Which runner holds the conversation: the flag when given, else the
    tier's adapter decides. Only the Claude Agent SDK tier can drive the SDK
    runner; every other adapter is a ``complete()`` adapter, which is exactly
    what the in-repo loop runs on."""
    if override:
        return override
    return "sdk" if adapter_name == "claude_agent_sdk" else "loop"


def build_runner(kind: str, resolved: ResolvedModel, spec: LaneSpec) -> Runner:
    if kind == "sdk":
        from core.llm.adapters.claude_sdk import ClaudeAgentSdkRunner

        return ClaudeAgentSdkRunner()
    if kind == "replay":
        from core.llm.adapters.fixture import FixtureAdapter

        path = spec.path("replay")
        if path is None:
            raise LaneError("--adapter replay needs --param replay=<recorded turns json>")
        try:
            turns = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LaneError(f"--param replay={path}: {exc}") from exc
        replies = [t if isinstance(t, str) else json.dumps(t) for t in turns]
        return GatewayLoopRunner(FixtureAdapter(replies), pricing=resolved.pricing)
    return GatewayLoopRunner(build_adapter(resolved), pricing=resolved.pricing)


# ---- the command ---------------------------------------------------------------


def run_lane(
    tenant: str,
    skill_path: str | Path,
    *,
    params: list[str],
    adapter: str | None = None,
    as_json: bool = False,
    out: TextIO | None = None,
) -> int:
    """Run one session and report it. Returns the process exit code."""
    stream = sys.stdout if out is None else out
    spec = build_spec(params)
    skill = Skill.load(Path(skill_path).expanduser())
    settings = load_tenant(tenant).llm
    resolved = resolve(settings, job_type_for(Path(skill_path), spec))
    kind = runner_kind_for(resolved.adapter, adapter)
    runner = build_runner(kind, resolved, spec)
    result = run(
        skill,
        build_context(spec),
        build_allowlist(spec),
        runner=runner,
        budget=build_budget(spec),
        model=resolved.model,
        transcript_dir=transcript_dir(spec),
        pricing=resolved.pricing,
        redact_env=[resolved.api_key_env] if resolved.api_key_env else [],
    )
    note_path = spec.path("note")
    written = False
    if note_path is not None and result.status == "FINISHED":
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(result.note, encoding="utf-8")
        written = True
    if as_json:
        print(result.model_dump_json(indent=2), file=stream)
    else:
        _report(result, skill, tenant, note_path, written, file=stream)
    if result.status == "FINISHED":
        return EXIT_OK
    return EXIT_SKIPPED if result.status == "SKIPPED" else EXIT_FAILED


def _report(
    result: RunnerResult,
    skill: Skill,
    tenant: str,
    note_path: Path | None,
    written: bool,
    *,
    file: TextIO,
) -> None:
    print(f"runner/{skill.name} @ {tenant}: {result.status} ({result.stop_reason})", file=file)
    print(
        f"  runner: {result.runner}  model: {result.model}  turns: {result.turns}  "
        f"tokens: {result.input_tokens} in / {result.output_tokens} out  "
        f"usd: {result.usd if result.usd is not None else 'unpriced'}  "
        f"wall: {result.wall_seconds}s",
        file=file,
    )
    print(
        f"  files changed: {len(result.files_changed)}  commands: {len(result.commands_run)}  "
        f"refused: {len(result.refused)}",
        file=file,
    )
    for refusal in result.refused:
        print(f"  refused {refusal.tool}: {refusal.reason}", file=file)
    print(f"  transcript: {result.transcript_path}", file=file)
    if note_path is not None:
        print(f"  note: {note_path} ({'written' if written else 'NOT written'})", file=file)
    if result.status != "FINISHED" and result.note:
        print("  last progress:", file=file)
        for line in result.note.splitlines():
            print(f"    {line}", file=file)
