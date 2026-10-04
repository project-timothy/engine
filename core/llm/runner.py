"""The runner: one agentic session, one function, one result
(``docs/runner-design.md``; decision
``docs/decisions/2026-09-12-runner-contract.md``, a one-way door).

Every agentic lane (triage, the build lane, proposal PRs) goes through
:func:`run`, which owns what must be identical whatever runner executes:

- the :class:`~core.llm.tools.ToolGate` (every tool call passes one Python
  check before it executes; a refusal is recorded and returned to the model);
- the JSONL transcript with its redaction (``core.llm.transcript``);
- the budget clock (turns, seconds, usd, tokens);
- the inputs digest (skill sha, bundle digest, allowlist digest, runner
  name, model id, runner version), which names the transcript and rides the
  result so the calling job's ``RunKey`` can fold it in;
- the result: FINISHED, SKIPPED, or FAILED with a named stop reason, the
  note, files changed by worktree hash, commands run, refusals, tokens,
  usd, turns, wall seconds, transcript path.

A :class:`Runner` owns only how a conversation is held. Two ship:
:class:`GatewayLoopRunner` here (one ``complete()`` per turn against the
:class:`Turn` output model, the always-available floor) and
``core.llm.adapters.claude_sdk.ClaudeAgentSdkRunner`` (the Claude Agent SDK,
imported lazily). Unwired in this row: rows 7.17 and 7.6 call it.

No model name lives here; the caller passes the id the tenant policy
resolved.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from core.llm.gateway import (
    GatewayTransportError,
    GatewayValidationError,
    Message,
    Pricing,
    complete,
)
from core.llm.tools import (
    CommandRun,
    FileChange,
    Gh,
    Git,
    ListFiles,
    Pytest,
    ReadFile,
    RefusedCall,
    Shell,
    ToolAllowlist,
    ToolGate,
    WriteFile,
    diff_trees,
    hash_tree,
)
from core.llm.transcript import Transcript, redact_text

RUNNER_VERSION = "1"
UNFINISHED_MARKERS = ("preliminary",)
Status = Literal["FINISHED", "SKIPPED", "FAILED"]

# ---- the skill --------------------------------------------------------------


class SkillFormatError(ValueError):
    """The ``SKILL.md`` frontmatter is not flat ``key: value`` lines, or a
    required field is missing."""


class Skill(BaseModel):
    """A skill file in the Agent Skills format: ``---`` frontmatter with at
    least ``name`` and ``description``, then the markdown body that becomes
    the system prompt. Frontmatter is parsed as flat ``key: value`` lines
    only (no YAML dependency); nested or list values raise
    :class:`SkillFormatError`. Other fields ride along as strings."""

    model_config = ConfigDict(frozen=True)

    path: Path
    name: str
    description: str
    frontmatter: dict[str, str]
    body: str
    sha256: str

    @classmethod
    def load(cls, path: Path) -> Skill:
        path = Path(path)
        raw = path.read_bytes()
        frontmatter, body = parse_frontmatter(raw.decode("utf-8"), path)
        for required in ("name", "description"):
            if not frontmatter.get(required):
                raise SkillFormatError(f"{path}: frontmatter needs {required!r}")
        return cls(
            path=path,
            name=frontmatter["name"],
            description=frontmatter["description"],
            frontmatter=frontmatter,
            body=body,
            sha256=hashlib.sha256(raw).hexdigest(),
        )


def parse_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError(f"{path}: no frontmatter (the file must open with ---)")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise SkillFormatError(f"{path}: frontmatter never closes") from exc
    fields: dict[str, str] = {}
    for n, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            raise SkillFormatError(f"{path}:{n}: nested frontmatter is not supported")
        if line.lstrip().startswith("- "):
            raise SkillFormatError(f"{path}:{n}: list frontmatter is not supported")
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            raise SkillFormatError(f"{path}:{n}: expected 'key: value'")
        value = value.strip()
        if value in ("", "|", ">", "|-", ">-"):
            raise SkillFormatError(f"{path}:{n}: {key.strip()!r} has no flat value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return fields, body


# ---- the context bundle -----------------------------------------------------


class ContextItem(BaseModel):
    """One input: inline text or a file. A file item keeps the path and the
    bytes' sha256; rendering re-reads the file and refuses to proceed when
    the bytes moved under it (the digest would be a lie)."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["text", "file"]
    text: str | None = None
    path: Path | None = None
    sha256: str

    @classmethod
    def from_text(cls, name: str, text: str) -> ContextItem:
        return cls(name=name, kind="text", text=text, sha256=_sha256(text.encode("utf-8")))

    @classmethod
    def from_file(cls, name: str, path: Path) -> ContextItem:
        path = Path(path)
        return cls(name=name, kind="file", path=path, sha256=_sha256(path.read_bytes()))

    def content(self) -> str:
        if self.kind == "text":
            return self.text or ""
        data = Path(self.path).read_bytes()
        if _sha256(data) != self.sha256:
            raise ValueError(
                f"context item {self.name!r}: {self.path} changed since the bundle was built"
            )
        return data.decode("utf-8", errors="replace")


class ContextBundle(BaseModel):
    """The deterministic inputs. ORDER IS PART OF THE CONTRACT: the digest
    and the rendered prompt both follow ``items`` as given."""

    items: list[ContextItem] = []

    def digest(self) -> str:
        rows = [(i.name, i.kind, i.sha256) for i in self.items]
        return _sha256(json.dumps(rows).encode("utf-8"))

    def render(self) -> str:
        """The one user prompt: every item as a section, file items inlined
        (every consumer named in the design note hands over text)."""
        parts: list[str] = []
        for item in self.items:
            label = (
                item.name if item.kind == "text" else f"{item.name} (file: {Path(item.path).name})"
            )
            parts.append(f"## {label}\n\n{item.content()}")
        return "\n\n".join(parts)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- the budget and the result ----------------------------------------------


class Budget(BaseModel):
    max_turns: int
    max_seconds: int
    max_usd: Decimal | None = None
    max_tokens: int | None = None
    """Input plus output, summed over the session."""


class RunnerResult(BaseModel):
    status: Status
    stop_reason: str
    """``note`` (FINISHED), ``precondition`` (SKIPPED), or the FAILED cause:
    ``turns``, ``seconds``, ``usd``, ``tokens``, ``transport``,
    ``validation``, ``unfinished`` (a note whose first line still says
    preliminary)."""
    note: str = ""
    files_changed: list[FileChange] = []
    commands_run: list[CommandRun] = []
    refused: list[RefusedCall] = []
    input_tokens: int = 0
    output_tokens: int = 0
    usd: Decimal | None = None
    turns: int = 0
    wall_seconds: int = 0
    model: str = ""
    runner: str = ""
    inputs_digest: str = ""
    transcript_path: Path | None = None
    prior_transcript: bool = False
    """A transcript with the same inputs digest already existed (it was
    moved aside, never deleted). The runner never skips on it; the calling
    job's ``RunKey`` does."""


# ---- the turn protocol (the loop runner's output model) ---------------------


class Turn(BaseModel):
    """One model turn: exactly one tool call or the final note.
    ``progress`` is the running note a budget stop keeps; ``reason`` is
    one sentence for the transcript."""

    reason: str = ""
    progress: str | None = None
    call: ReadFile | WriteFile | ListFiles | Shell | Git | Gh | Pytest | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Turn:
        if (self.call is None) == (self.note is None):
            raise ValueError("a turn carries exactly one of call or note")
        return self


def unfinished(note: str) -> bool:
    head = next((line for line in note.splitlines() if line.strip()), "").lower()
    return any(marker in head for marker in UNFINISHED_MARKERS)


# ---- the session and the runner protocol ------------------------------------


@dataclass
class Session:
    """The runner-facing view of one run. Runners read the inputs, charge
    tokens and usd, count turns, and ask the clock before each turn and
    after each tool; ``run()`` does the rest."""

    skill: Skill
    context: ContextBundle
    allowlist: ToolAllowlist
    gate: ToolGate
    budget: Budget
    transcript: Transcript
    model: str
    pricing: Pricing | None
    clock: Callable[[], float]
    started: float
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: Decimal | None = None
    progress: str = ""
    provider_model: str | None = None
    _stop: str | None = field(default=None, init=False)

    def elapsed(self) -> float:
        return self.clock() - self.started

    def charge(self, input_tokens: int, output_tokens: int, usd: Decimal | None) -> None:
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        if usd is not None:
            self.usd = (self.usd or Decimal(0)) + usd

    def over_budget(self) -> str | None:
        """The budget name exhausted right now (seconds, usd, tokens), else
        ``None``. Turns are checked by :meth:`begin_turn`."""
        b = self.budget
        if self.elapsed() > b.max_seconds:
            return "seconds"
        if b.max_usd is not None and self.usd is not None and self.usd > b.max_usd:
            return "usd"
        if b.max_tokens is not None and self.input_tokens + self.output_tokens > b.max_tokens:
            return "tokens"
        return None

    def begin_turn(self) -> str | None:
        """Count one turn, or name the budget that forbids it."""
        if self.turns >= self.budget.max_turns:
            return "turns"
        stop = self.over_budget()
        if stop:
            return stop
        self.turns += 1
        return None

    def _base(self, status: Status, stop_reason: str, note: str) -> RunnerResult:
        return RunnerResult(
            status=status,
            stop_reason=stop_reason,
            note=note,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            usd=self.usd,
            turns=self.turns,
            model=self.model,
        )

    def failed(self, stop_reason: str, note: str | None = None) -> RunnerResult:
        return self._base("FAILED", stop_reason, self.progress if note is None else note)

    def finished(self, note: str) -> RunnerResult:
        if unfinished(note):
            return self._base("FAILED", "unfinished", note)
        return self._base("FINISHED", "note", note)

    def skipped(self, reason: str) -> RunnerResult:
        return self._base("SKIPPED", "precondition", reason)


class Runner(Protocol):
    name: str
    usd_known: bool
    """Whether the runner reports usd without a ``Pricing`` (the SDK does;
    the loop needs the policy's pricing). ``max_usd`` with neither is a
    SKIPPED precondition."""

    def run(self, session: Session) -> RunnerResult: ...


# ---- the run key ------------------------------------------------------------


def inputs_digest(
    skill: Skill, context: ContextBundle, allowlist: ToolAllowlist, runner_name: str, model: str
) -> str:
    parts = [skill.sha256, context.digest(), allowlist.digest(), runner_name, model, RUNNER_VERSION]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ---- the entry point ---------------------------------------------------------


def run(
    skill: Skill,
    context: ContextBundle,
    allowlist: ToolAllowlist,
    *,
    runner: Runner,
    budget: Budget,
    model: str,
    transcript_dir: Path,
    pricing: Pricing | None = None,
    redact_env: list[str] | tuple[str, ...] = (),
    clock: Callable[[], float] = time.monotonic,
) -> RunnerResult:
    """Run one session and return its result. ``model`` is the id the tenant
    policy resolved; ``pricing`` is that tier's price list (the loop runner
    needs it to report usd); ``redact_env`` names the environment variables
    whose values must never reach the transcript (the tenant's provider
    keys). ``clock`` is injectable for the budget tests."""
    digest = inputs_digest(skill, context, allowlist, runner.name, model)
    transcript_dir = Path(transcript_dir)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{digest}.jsonl"
    prior = _move_prior_aside(path)
    transcript = Transcript(path, env_names=redact_env)
    started = clock()
    roots = allowlist.roots()
    before = {str(r): hash_tree(r) for r in roots}
    transcript.write(
        "run_start",
        {
            "skill": {"name": skill.name, "path": str(skill.path), "sha256": skill.sha256},
            "context_digest": context.digest(),
            "allowlist_digest": allowlist.digest(),
            "runner": runner.name,
            "model": model,
            "budget": budget.model_dump(mode="json"),
            "inputs_digest": digest,
            "prior_transcript": prior,
        },
    )
    gate = ToolGate(allowlist, transcript=transcript)
    session = Session(
        skill=skill,
        context=context,
        allowlist=allowlist,
        gate=gate,
        budget=budget,
        transcript=transcript,
        model=model,
        pricing=pricing,
        clock=clock,
        started=started,
    )
    if budget.max_usd is not None and pricing is None and not runner.usd_known:
        result = session.skipped(
            "max_usd is set but no pricing was given, so usd cannot be counted"
        )
    else:
        result = runner.run(session)

    changes: list[FileChange] = []
    for root in roots:
        changes.extend(diff_trees(before[str(root)], hash_tree(root)))
    values = transcript._values
    result = result.model_copy(
        update={
            "note": redact_text(result.note, values),
            "files_changed": changes,
            "commands_run": list(gate.commands_run),
            "refused": [
                r.model_copy(update={"reason": redact_text(r.reason, values)}) for r in gate.refused
            ],
            "wall_seconds": int(clock() - started),
            "model": result.model or model,
            "runner": runner.name,
            "inputs_digest": digest,
            "transcript_path": path,
            "prior_transcript": prior,
        }
    )
    transcript.write(
        "run_end", {"result": result.model_dump(mode="json", exclude={"transcript_path"})}
    )
    return result


def _move_prior_aside(path: Path) -> bool:
    if not path.exists():
        return False
    n = 1
    while (aside := path.with_name(f"{path.stem}.prior{n}.jsonl")).exists():
        n += 1
    path.rename(aside)
    return True


# ---- the in-repo loop runner ------------------------------------------------

_TOOL_MANUAL = {
    "read_file": '{"tool": "read_file", "path": "<path inside the root>"}: returns the file text.',
    "write_file": '{"tool": "write_file", "path": "<path inside the root>", '
    '"content": "<whole file>"}: writes the file (parents created; never under .git/).',
    "list_files": '{"tool": "list_files", "glob": "<glob relative to the root>"}: '
    "lists matching files.",
    "shell": '{"tool": "shell", "argv": ["<program>", "<arg>", ...]}: one plain argv, no shell '
    "operators, cwd = the root. Allowed prefixes: {prefixes}.",
    "git": '{"tool": "git", "args": ["<subcommand>", ...]}: subcommands {subcommands}; push only '
    'as ["push", "-u", "origin", "<current branch>"].',
    "gh": '{"tool": "gh", "args": ["pr", "create", ...]}: subcommands {subcommands}{gates}.',
    "pytest": '{"tool": "pytest", "args": [...]}: runs `uv run pytest` in the root; args are '
    "paths inside the root and -q, -x, -k <expr>, -p no:cacheprovider.",
}


def tool_manual(allowlist: ToolAllowlist) -> str:
    """The tools the model is told about: only those the allowlist admits."""
    lines = [
        "## Tools",
        "",
        "Reply with one JSON object per turn: exactly one of `call` (one tool call) or `note` "
        "(the final note, the deliverable). Put the running state of your work in `progress` on "
        "every turn: if a budget stops the session, `progress` is what survives. A refused call "
        "comes back with ok=false and the reason; route around it or write the note.",
        "",
    ]
    for name in allowlist.tool_names():
        text = _TOOL_MANUAL[name]
        if name == "shell":
            prefixes = ", ".join(" ".join(p) for p in allowlist.shell.argv_prefixes)
            text = text.replace("{prefixes}", prefixes)
        elif name == "git":
            text = text.replace("{subcommands}", ", ".join(allowlist.git.subcommands))
        elif name == "gh":
            gates = (
                "; `pr create` needs a new or changed test file and a passing pytest run "
                "after your last write_file (eval_first)"
                if "eval_first" in allowlist.gh.gates
                else ""
            )
            text = text.replace("{subcommands}", ", ".join(allowlist.gh.subcommands))
            text = text.replace("{gates}", gates)
        lines.append(f"- {text}")
    return "\n".join(lines)


class GatewayLoopRunner:
    """One ``complete()`` per turn against :class:`Turn`; the gate executes
    the call and its result becomes the next user message. The always
    available floor: any adapter with a JSON mode can drive it."""

    name = "gateway_loop"

    @property
    def usd_known(self) -> bool:
        return self._pricing is not None

    def __init__(
        self,
        adapter,
        *,
        pricing: Pricing | None = None,
        job_type: str = "agent_session",
        timeout_s: int = 300,
        output_cap: int = 16 * 1024,
    ) -> None:
        self._adapter = adapter
        self._pricing = pricing
        self._job_type = job_type
        self._timeout_s = timeout_s
        self._output_cap = output_cap

    def run(self, session: Session) -> RunnerResult:
        pricing = session.pricing if session.pricing is not None else self._pricing
        session.pricing = pricing
        system = f"{session.skill.body.rstrip()}\n\n{tool_manual(session.allowlist)}"
        user = session.context.render()
        session.transcript.write("prompt", {"system": system, "user": user})
        messages = [Message("system", system), Message("user", user)]
        while True:
            stop = session.begin_turn()
            if stop:
                return session.failed(stop)
            try:
                result = complete(
                    self._job_type,
                    messages,
                    Turn,
                    adapter=self._adapter,
                    model=session.model,
                    timeout_s=self._timeout_s,
                    pricing=pricing,
                )
            except GatewayValidationError as exc:
                session.transcript.write(
                    "turn",
                    {
                        "n": session.turns,
                        "raw": exc.replies[-1] if exc.replies else "",
                        "invalid": exc.errors,
                    },
                )
                return session.failed("validation")
            except GatewayTransportError as exc:
                session.transcript.write(
                    "turn", {"n": session.turns, "raw": "", "transport_error": str(exc)}
                )
                return session.failed("transport")
            record = result.record
            session.charge(record.input_tokens, record.output_tokens, record.usd)
            session.provider_model = record.provider_model or session.provider_model
            turn = result.output
            raw = turn.model_dump_json(exclude_none=True)
            session.transcript.write(
                "turn", {"n": session.turns, "raw": raw, "record": asdict(record)}
            )
            if turn.progress:
                session.progress = turn.progress
            if turn.note is not None:
                return session.finished(turn.note)
            tool_result = session.gate.call(turn.call, turn=session.turns)
            messages.append(Message("assistant", raw))
            messages.append(Message("user", tool_result.for_model(self._output_cap)))
            stop = session.over_budget()
            if stop:
                return session.failed(stop)
