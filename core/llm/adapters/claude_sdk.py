"""The Claude Agent SDK runner (``docs/runner-design.md``, "The two runners").

The SDK (``claude_agent_sdk``, the ``[claude]`` extra, row 7.15) is
imported lazily inside :func:`sdk` through ``core.llm.sdk.import_sdk``; when
it is absent the runner returns SKIPPED ``precondition`` and touches
nothing. The skill body is the system
prompt, the rendered bundle is the one user prompt, ``cwd`` is the file
root, and no settings sources or skills are loaded: the skill file is the
whole brief.

Enforcement, verified against the pinned SDK (0.2.152): ``query()`` routes
both ``can_use_tool`` and ``hooks`` to the transport, so the one-shot call
the extraction sites already use is enough. But ``can_use_tool`` fires only
when the CLI would otherwise ASK: a tool named in ``allowed_tools`` is
pre-approved and shadows the callback (the SDK warns), and a read the CLI
auto-approves never reaches it. So the gate sits in a ``PreToolUse`` hook,
which the SDK documents as the way to gate every call regardless of
permission rules; ``allowed_tools`` stays EMPTY so nothing is pre-approved;
``disallowed_tools`` removes every SDK tool the allowlist does not map; and
``can_use_tool`` answers the ask from the verdict the hook already recorded,
so a headless session never blocks on a prompt. Every SDK call maps to one
of the runner's tool models (:func:`sdk_call_to_tool_call`) and the same
``ToolGate.check`` judges it; a ``Bash`` command is split with ``shlex``
into one argv and shell operators are refused, the loop's rule.

The message stream is written to the transcript event by event; tokens
come from the SDK's result usage and usd from its ``total_cost_usd``.
"""

from __future__ import annotations

import asyncio
import shlex
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from core.llm.runner import RunnerResult, Session
from core.llm.sdk import import_sdk
from core.llm.tools import (
    PYTEST_ARGV,
    Gh,
    Git,
    ListFiles,
    Pytest,
    ReadFile,
    Shell,
    ToolAllowlist,
    Verdict,
    WriteFile,
    shell_expansion,
    shell_operator,
)

SDK_BUFFER_BYTES = 32 * 1024 * 1024
"""Incident 2026-09-04: the CLI's stdout buffer must cover a whole reply."""

READ_TOOLS = ("Read", "Glob", "Grep")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOL = "Bash"
KNOWN_TOOLS = (
    *READ_TOOLS,
    *WRITE_TOOLS,
    SHELL_TOOL,
    "Agent",
    "Task",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Skill",
    "KillShell",
    "BashOutput",
    "ExitPlanMode",
    "AskUserQuestion",
    "SlashCommand",
    "TaskOutput",
)


def sdk():
    """The lazy import seam. Tests stand in for it to prove the SKIPPED path;
    without the extra it raises ``SdkMissing`` (a ``ModuleNotFoundError``)."""
    return import_sdk("the Claude Agent SDK runner")


def stream(prompt: str, options: Any):
    """The SDK's one-shot query as an async iterator of messages. Module
    level so a test can replay a recorded stream through the same runner."""
    return sdk().query(prompt=prompt, options=options)


def admitted_sdk_tools(allowlist: ToolAllowlist) -> list[str]:
    names: list[str] = []
    if allowlist.files:
        names += list(READ_TOOLS)
        if allowlist.files.write:
            names += list(WRITE_TOOLS)
    if allowlist.shell or allowlist.git or allowlist.gh or allowlist.pytest:
        names.append(SHELL_TOOL)
    return names


def sdk_call_to_tool_call(name: str, tool_input: dict[str, Any]) -> tuple[Any, str]:
    """Map an SDK tool use onto the runner's tool models. Returns
    ``(call, "")`` or ``(None, reason)`` when the call cannot be expressed
    (an unknown tool, a Bash line with operators, ``gh pr merge``)."""
    try:
        if name in ("Read", "Grep"):
            return ReadFile(
                tool="read_file",
                path=str(tool_input.get("file_path") or tool_input.get("path") or "."),
            ), ""
        if name == "Glob":
            pattern = str(tool_input.get("pattern", "**/*"))
            base = tool_input.get("path")
            return ListFiles(tool="list_files", glob=f"{base}/{pattern}" if base else pattern), ""
        if name in WRITE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            content = (
                tool_input.get("content")
                or tool_input.get("new_string")
                or tool_input.get("new_source")
                or ""
            )
            return WriteFile(tool="write_file", path=str(path), content=str(content)), ""
        if name == SHELL_TOOL:
            return _bash(str(tool_input.get("command", "")))
    except ValidationError as exc:
        return None, "; ".join(e.get("msg", "") for e in exc.errors()) or str(exc)
    return None, f"{name} is not in the allowlist (no runner tool maps to it)"


def _bash(command: str) -> tuple[Any, str]:
    hazard = shell_expansion(command)
    if hazard:
        # shlex reads an unquoted newline as whitespace and leaves $VAR and ~
        # alone; the shell runs a second command or the expansion (security
        # review 2026-10-03, finding 5).
        return None, (
            f"{hazard!r} outside quotes in the Bash command; the shell would split or "
            "expand it. one plain argv only: single-quote literal text, or use a body file"
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, f"Bash command is not one plain argv: {exc}"
    if not argv:
        return None, "Bash command is empty"
    bad = shell_operator(argv, shell=True)
    if bad:
        return None, f"shell operator {bad!r} in the Bash command; one plain argv only"
    if argv[0] == "git":
        return Git(tool="git", args=argv[1:]), ""
    if argv[0] == "gh":
        return Gh(tool="gh", args=argv[1:]), ""
    if tuple(argv[:3]) == PYTEST_ARGV:
        return Pytest(tool="pytest", args=argv[3:]), ""
    if argv[0] == "pytest":
        return Pytest(tool="pytest", args=argv[1:]), ""
    return Shell(tool="shell", argv=argv), ""


class ClaudeAgentSdkRunner:
    name = "claude_agent_sdk"
    usd_known = True

    def __init__(self, *, max_buffer_size: int = SDK_BUFFER_BYTES) -> None:
        self._max_buffer_size = max_buffer_size

    def run(self, session: Session) -> RunnerResult:
        try:
            mod = sdk()
        except ModuleNotFoundError:
            return session.skipped("the Claude Agent SDK is not installed (the [claude] extra)")
        options = self._options(mod, session)
        return asyncio.run(self._drive(mod, session, options))

    # -- options and callbacks --------------------------------------------------

    def _admit(self, session: Session, name: str, tool_input: dict[str, Any], ref: str | None):
        gate = session.gate
        if ref is not None:
            known = gate.verdict_for(ref)
            if known is not None:
                return known
        call, reason = sdk_call_to_tool_call(name, tool_input)
        if call is None:
            gate.refuse(
                turn=session.turns, tool=name, target=_target(tool_input), reason=reason, ref=ref
            )
            return gate.verdict_for(ref) if ref is not None else Verdict(False, reason)
        return gate.admit(call, turn=session.turns, ref=ref)

    def _options(self, mod, session: Session):
        runner = self

        async def pre_tool_use(input_data, tool_use_id, context):
            name = str(input_data.get("tool_name", ""))
            tool_input = dict(input_data.get("tool_input") or {})
            ref = tool_use_id or input_data.get("tool_use_id")
            verdict = runner._admit(session, name, tool_input, ref)
            if verdict.allowed:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": verdict.reason,
                }
            }

        async def can_use_tool(tool_name, tool_input, context):
            ref = getattr(context, "tool_use_id", None)
            verdict = runner._admit(session, str(tool_name), dict(tool_input or {}), ref)
            if verdict.allowed:
                return mod.PermissionResultAllow()
            return mod.PermissionResultDeny(message=verdict.reason)

        admitted = admitted_sdk_tools(session.allowlist)
        roots = session.allowlist.roots()
        return mod.ClaudeAgentOptions(
            system_prompt=session.skill.body,
            cwd=str(roots[0]) if roots else None,
            setting_sources=[],
            skills=[],
            allowed_tools=[],
            disallowed_tools=[t for t in KNOWN_TOOLS if t not in admitted],
            max_turns=session.budget.max_turns,
            max_buffer_size=self._max_buffer_size,
            model=session.model,
            can_use_tool=can_use_tool,
            hooks={"PreToolUse": [mod.HookMatcher(matcher=None, hooks=[pre_tool_use])]},
        )

    # -- the stream --------------------------------------------------------------

    async def _drive(self, mod, session: Session, options) -> RunnerResult:
        user = session.context.render()
        session.transcript.write("prompt", {"system": session.skill.body, "user": user})
        result_message = None
        stop: str | None = None
        messages = stream(user, options)
        try:
            async for message in messages:
                if isinstance(message, mod.AssistantMessage):
                    stop = session.begin_turn()
                    if stop:
                        break
                    texts = [b.text for b in message.content if isinstance(b, mod.TextBlock)]
                    if texts:
                        session.progress = "\n".join(texts)
                    usage = message.usage or {}
                    session.charge(
                        usage.get("input_tokens", 0), usage.get("output_tokens", 0), None
                    )
                    session.provider_model = (
                        getattr(message, "model", None) or session.provider_model
                    )
                    session.transcript.write(
                        "turn",
                        {
                            "n": session.turns,
                            "raw": _serialize_assistant(mod, message),
                            "record": {"usage": usage, "model": message.model},
                        },
                    )
                elif isinstance(message, mod.UserMessage):
                    for block in message.content if isinstance(message.content, list) else []:
                        if isinstance(block, mod.ToolResultBlock):
                            session.gate.settle(
                                block.tool_use_id,
                                ok=not block.is_error,
                                output=_text(block.content),
                            )
                elif isinstance(message, mod.ResultMessage):
                    result_message = message
                    session.transcript.write(
                        "sdk_result",
                        {
                            "subtype": message.subtype,
                            "is_error": message.is_error,
                            "num_turns": message.num_turns,
                            "usage": message.usage,
                            "total_cost_usd": message.total_cost_usd,
                        },
                    )
                stop = session.over_budget()
                if stop:
                    break
        finally:
            aclose = getattr(messages, "aclose", None)
            if aclose is not None:
                await aclose()
        if stop:
            return session.failed(stop)
        if result_message is None:
            return session.failed("transport", session.progress)
        usage = result_message.usage or {}
        if usage:
            session.input_tokens = int(usage.get("input_tokens", session.input_tokens))
            session.output_tokens = int(usage.get("output_tokens", session.output_tokens))
        if result_message.total_cost_usd is not None:
            session.usd = Decimal(str(result_message.total_cost_usd))
        if result_message.subtype == "error_max_turns":
            return session.failed("turns")
        if result_message.is_error:
            return session.failed("transport")
        note = result_message.result or session.progress
        return session.finished(note)


def _target(tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "prompt"):
        if key in tool_input:
            return str(tool_input[key])[:200]
    return ""


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return "" if content is None else str(content)


def _serialize_assistant(mod, message) -> str:
    """The assistant message as replayable JSON text: text blocks and tool
    uses, nothing provider-private."""
    import json

    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, mod.TextBlock):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, mod.ToolUseBlock):
            blocks.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return json.dumps(blocks)
