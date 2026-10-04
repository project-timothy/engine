"""The Claude Agent SDK as a ``complete()`` adapter (phase 7 row 7.10).

Row 7.9 landed the policy with ``claude_agent_sdk`` as a DESCRIBE-ONLY tier
name: a tenant could state "what I run today is the SDK under a seat" but the
policy refused to build it, so no job could be pointed through the policy at
that seat. That blocked row 7.10: moving AP extraction behind the gateway
would have moved it onto a metered tier and changed what the owner pays. This
adapter closes the gap. It makes the same call
``core.agents.ap.extraction.ClaudeExtractor`` made: one ``query()`` with the
document-reading tools and a 32 MB transport buffer. The one setting that moved
is the turn bound, and it moved because the old value was demonstrably too low
(:data:`MAX_TURNS`, docs/lessons.md, "Budget for the hardest input").

**This session reads and does nothing else** (issue #296). It used to run
Bash, and honestly so: ``allowed_tools`` is a PRE-APPROVAL list in the Claude
Agent SDK, not a restriction, a tool left off it is unapproved rather than
refused, and the live sessions of 2026-09-16 rasterized an image-only PDF
through Bash and read the pages back, which was the only way that class of
document extracted at all (issue #265). The ENGINE renders those pages now
(:mod:`core.llm.rasterize`), into this call's own scratch directory, so there
is nothing left to shell out for:

- ``allowed_tools`` is :data:`SESSION_TOOLS`, ``Read`` alone, and only when
  the bundle carries an attachment;
- :data:`DENIED_TOOLS` goes to ``disallowed_tools``, which the SDK documents as
  removing a tool from the session's context outright. That is the difference
  between a confinement and a wish, and it is why the denial is spelled rather
  than left to the gap in the pre-approval list. A ``can_use_tool`` callback
  cannot do this job here: the SDK itself warns that a whole-tool
  ``allowed_tools`` entry (``Read``) auto-approves before any callback runs;
- every call with an attachment still gets its own named directory under the
  engine's state root (:func:`resolve_scratch_root`), because that is where the
  rendered pages go, and it is still inventoried, recorded and swept when the
  call returns.

The sibling adapters have no tools at all: ``anthropic_messages`` and
``openai_compat`` inline the pages (or the document) on the wire and nothing
executes anywhere.

The mapping from :class:`~core.llm.gateway.PromptBundle` to the SDK:

- ``system_text()`` and the turns fold into the ONE prompt string the
  one-shot ``query()`` takes (the SDK has no separate system slot on this
  path, and the gateway already put the JSON schema into a system turn, so
  the schema reaches the model as text like every other adapter);
- each :class:`~core.llm.gateway.Attachment` becomes a "Read the document at
  <path>" line in front of the first user turn and switches :data:`SESSION_TOOLS`
  on. The SDK reads files off disk, which is what "attachments as the SDK
  expects" means here: nothing is base64-inlined;
- an attachment with no text layer is rendered to page images first, and the
  read lines name those instead, with one line of prose saying what they are
  and which pages they are not (:meth:`~core.llm.rasterize.RenderedPages.note`).
  A render that cannot happen changes nothing: the file goes over as it always
  did and comes back ``needs_ocr`` if nobody can read it;
- an attachment also gets the session its scratch directory and the line that
  names it; a bundle with no attachment gets no tools and no directory;
- ``timeout_s`` bounds the whole call. A stalled transport raises
  ``TimeoutError``, which the gateway maps to a transient ``timeout``
  (docs/lessons.md, "A declared bound is an enforced bound").

The seat is flat rate, so its tier prices at zero and a call through it
records zero usd while still recording the tokens the SDK reports. The SDK
itself is the optional ``[claude]`` extra (row 7.15), imported lazily through
:func:`core.llm.sdk.import_sdk`; without it the adapter raises a NON-transient
``sdk_missing`` transport failure, because no redial installs a package.

No model id lives here: ``bundle.model`` comes from the tenant policy. The one
sentinel is :data:`DEFAULT_MODEL`, the tenant's spelling for "whatever the CLI
is logged in as", which omits the option the way the extractor did with
``model=None``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.llm.gateway import GatewayTransportError, PromptBundle, RawReply, Usage
from core.llm.rasterize import PAGES_DIRNAME, RenderedPages, render_attachment, write_pages
from core.llm.sdk import SdkMissing, import_sdk

SITE = "the Claude Agent SDK gateway adapter"

DEFAULT_MODEL = "default"
"""A tier whose ``model`` is this (or empty) leaves the model unset, so the
CLI's own default answers. Not a model id: the sentinel for "the seat"."""

SDK_BUFFER_BYTES = 32 * 1024 * 1024
"""The CLI transport inflates a Read result by ~4/3 (base64), so the buffer
clears the extractor's 20 MB file cap with headroom (docs/lessons.md,
"Oversize inputs fail fast", raised again by PR #184)."""

MAX_TURNS = 12
"""Reading a multi-page scanned PDF costs several tool turns before the model
has anything to answer from. The extractor's old bound of 4 made a whole class
of documents permanently unextractable: a 1.7 MB scanned fax receipt failed
every attempt on the live seat at 4 turns and again at 8 with the file alone
(docs/lessons.md, "Budget for the hardest input"). ``timeout_s`` is the real bound on a runaway
session; this one only stops an unbounded loop."""

READ_TOOL = "Read"
BASH_TOOL = "Bash"

SESSION_TOOLS = (READ_TOOL,)
"""What a session with a document is pre-approved for, and now all it can do.
Between 2026-09-17 and issue #296 this named ``Bash`` too, because the session
rendered its own pages and the list is honest about what runs. The engine
renders them now, so the reason is gone and the list is one tool again."""

DENIED_TOOLS = (
    BASH_TOOL,
    "BashOutput",
    "KillShell",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)
"""Refused outright through ``disallowed_tools``, which the SDK removes from
the session's context rather than merely leaving unapproved.

A denylist is what the option gives, so this names every shipped tool that
could run something on this host, write to it, or reach the network, not only
the one the live sessions happened to use. ``Task`` is here because a subagent
is a session of its own and these denials are not a promise it inherits.
Reading is the whole job: anything not on :data:`SESSION_TOOLS` and not here
would still have to get past an interactive permission prompt that nobody is
sitting at."""


# ---- the scratch directory (issue #265) --------------------------------------

SCRATCH_ROOT_ENV = "ENGINE_LLM_SCRATCH_ROOT"
"""Names the root directly, the way ``ENGINE_LEDGER_ROOT`` names the ledger's."""

DATA_ROOT_ENV = "ENGINE_DATA_ROOT"
"""The container's one volume (row 7.21): everything that is not code lives
under it, so the scratch root does too when the variable is set."""

SCRATCH_DIRNAME = "llm-scratch"
LOCAL_SCRATCH_DIRNAME = ".llm-scratch"
"""Beside the run when nothing names a root, the way ``.ledger`` and
``.auditor`` sit beside a run. Both are in ``.gitignore``, so a checkout the
freshness guard inspects stays clean."""

SCRATCH_LOG = "scratch-log.jsonl"
"""One line per call under the root: when, which job, which directory, what the
session left in it, and whether the sweep got it. The directory is gone by the
time anyone reads this, which is exactly why the line exists."""

SCRATCH_LABEL = "Scratch directory: "

SCRATCH_INSTRUCTION = (
    SCRATCH_LABEL + "{path}\n"
    "That directory is the engine's working area for this one call: any page "
    "images rendered from the document are in it. This session has Read and "
    "nothing else, so read what you need from there and write nothing anywhere "
    "on this host. The directory is inventoried and deleted when this call "
    "returns."
)
"""The prompt says what the directory is FOR. It stopped being a permission
when the engine took over the rendering (issue #296) and the tool list stopped
naming anything that writes. No shared temporary directory is named here or
anywhere else in this module."""

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def resolve_scratch_root(
    explicit: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """Where per-call scratch directories live, by the engine's own state
    convention (``core.engine.runner.resolve_ledger_root``): an explicit value
    wins, then :data:`SCRATCH_ROOT_ENV`, then the container's data volume, then
    a directory beside the run."""
    environment = os.environ if env is None else env
    if explicit is not None:
        return Path(explicit).expanduser()
    named = environment.get(SCRATCH_ROOT_ENV)
    if named:
        return Path(named).expanduser()
    data_root = environment.get(DATA_ROOT_ENV)
    if data_root:
        return Path(data_root).expanduser() / SCRATCH_DIRNAME
    return Path.cwd() / LOCAL_SCRATCH_DIRNAME


@dataclass(frozen=True)
class ScratchRecord:
    """What one call's directory was and what the session left in it. The
    adapter keeps the last one on itself and writes every one to
    :data:`SCRATCH_LOG`; a ``llm_calls`` column for it would be a ledger schema
    migration, which is the owner's one-way door, not this row's."""

    path: Path
    files: tuple[tuple[str, int], ...] = ()
    total_bytes: int = 0
    removed: bool = True


def open_scratch(root: Path, job_type: str) -> Path:
    """Make this call's directory: one per call, named for the job it serves,
    unguessable, owner-only."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    label = _SAFE_NAME.sub("-", job_type).strip("-") or "call"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = root / f"{label}-{stamp}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


def close_scratch(path: Path) -> ScratchRecord:
    """Inventory what the session left, then sweep the directory. Whatever the
    session rendered is a copy of a document the filing tree already holds, so
    the inventory is the part worth keeping and the bytes are not."""
    files: list[tuple[str, int]] = []
    for item in sorted(path.rglob("*")):
        if item.is_file() and not item.is_symlink():
            files.append((item.relative_to(path).as_posix(), item.stat().st_size))
    shutil.rmtree(path, ignore_errors=True)
    return ScratchRecord(
        path=path,
        files=tuple(files),
        total_bytes=sum(size for _, size in files),
        removed=not path.exists(),
    )


def log_scratch(root: Path, job_type: str, record: ScratchRecord) -> None:
    """Append the record to :data:`SCRATCH_LOG`. Bookkeeping never fails a
    call: an unwritable root loses the line, not the extraction."""
    row = {
        "at": datetime.now(UTC).isoformat(),
        "job_type": job_type,
        "dir": str(record.path),
        "files": [{"path": name, "bytes": size} for name, size in record.files],
        "bytes": record.total_bytes,
        "removed": record.removed,
    }
    try:
        with (Path(root) / SCRATCH_LOG).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def sdk():
    """The lazy import seam. Tests stand in for it; without the extra it
    raises :class:`~core.llm.sdk.SdkMissing`."""
    return import_sdk(SITE)


def readable_paths(bundle: PromptBundle, scratch: Path | None) -> frozenset[Path]:
    """The only files a session may ``Read``: its own attachments. Anything
    under its scratch directory (the rendered pages) is allowed separately."""
    return frozenset(Path(a.path).resolve() for a in bundle.attachments)


def read_refusal(
    name: str, tool_input: Mapping[str, Any], readable: frozenset[Path], scratch: Path | None
) -> str | None:
    """Why the hook refuses a tool use, or ``None`` to let it run. ``Read``
    of the call's attachments or its scratch is the whole allowlist; the
    denylist above stays as the first wall (security review 2026-10-03,
    finding 7: a document could otherwise ask for the token file)."""
    if name != READ_TOOL:
        return f"{name} is not available to a document-extraction session"
    raw = str(tool_input.get("file_path") or "")
    if not raw:
        return "Read needs a file_path"
    base = scratch if scratch is not None else Path.cwd()
    target = Path(raw) if Path(raw).is_absolute() else base / raw
    resolved = target.resolve()
    if resolved in readable:
        return None
    if scratch is not None and resolved.is_relative_to(Path(scratch).resolve()):
        return None
    return f"Read of {raw!r} is outside this document's files"


def stream(prompt: str, options: Any):
    """The SDK's one-shot query as an async iterator. Module level so a test
    can replay a recorded stream through the same adapter."""
    return sdk().query(prompt=prompt, options=options)


def read_lines(
    bundle: PromptBundle, pages: Mapping[Path, tuple[RenderedPages, tuple[Path, ...]]]
) -> list[str]:
    """What the session is told to read: the rendered pages of a scan, or the
    document itself for everything else."""
    lines: list[str] = []
    for attachment in bundle.attachments:
        rendered = pages.get(attachment.path)
        if rendered is None:
            lines.append(f"Read the document at {attachment.path}")
            continue
        page_set, written = rendered
        lines += [f"Read the page image at {path}" for path in written]
        lines.append(page_set.note())
    return lines


def build_prompt(
    bundle: PromptBundle,
    scratch: Path | None = None,
    pages: Mapping[Path, tuple[RenderedPages, tuple[Path, ...]]] | None = None,
) -> str:
    """One prompt string: the system text, then the turns, with the attachment
    read lines and the scratch directory in front of the first user turn."""
    blocks: list[str] = []
    system = bundle.system_text()
    if system:
        blocks.append(system)
    pending = read_lines(bundle, pages or {})
    if scratch is not None:
        pending.append(SCRATCH_INSTRUCTION.format(path=scratch))
    for turn in bundle.turns():
        if turn.role == "user" and pending:
            blocks.append("\n".join(pending))
            pending = []
        blocks.append(turn.content)
    if pending:  # attachments but no user turn: still name the files
        blocks.append("\n".join(pending))
    return "\n\n".join(blocks)


def _usage(message: Any) -> tuple[int, int] | None:
    reported = getattr(message, "usage", None)
    if not isinstance(reported, dict) or not reported:
        return None
    return int(reported.get("input_tokens", 0)), int(reported.get("output_tokens", 0))


class ClaudeSdkCompleteAdapter:
    """One ``complete()`` call = one one-shot SDK session."""

    name = "claude_agent_sdk"

    def __init__(
        self,
        *,
        max_buffer_size: int = SDK_BUFFER_BYTES,
        max_turns: int = MAX_TURNS,
        scratch_root: str | Path | None = None,
    ) -> None:
        self._max_buffer_size = max_buffer_size
        self._max_turns = max_turns
        self._scratch_root = scratch_root
        # The directory the last call gave its session, after the sweep. The
        # durable copy of the same thing is the SCRATCH_LOG line.
        self.last_scratch: ScratchRecord | None = None

    def complete(self, bundle: PromptBundle, schema: dict[str, Any]) -> RawReply:
        try:
            mod = sdk()
        except SdkMissing as exc:
            # Terminal: a redial cannot install a package. The message names
            # the extra, the same contract the extractor's sdk_missing cause
            # carried before this row.
            raise GatewayTransportError(str(exc), cause="sdk_missing", transient=False) from exc
        self.last_scratch = None
        # No document, no tools, no place to write: a text-only call gets no
        # directory at all.
        root = resolve_scratch_root(self._scratch_root) if bundle.attachments else None
        try:
            scratch = open_scratch(root, bundle.job_type) if root is not None else None
        except OSError as exc:
            # No writable directory means no place this session is allowed to
            # write, and the session would write somewhere anyway. Terminal on
            # purpose: a redial makes an unwritable root no more writable, and
            # the document goes to review naming the reason.
            raise GatewayTransportError(
                f"{bundle.job_type}: the model scratch root {root} is not writable ({exc}); "
                f"set {SCRATCH_ROOT_ENV} to a writable directory",
                cause="scratch_unavailable",
                transient=False,
            ) from exc
        try:
            options = self._options(mod, bundle, scratch)
            prompt = build_prompt(bundle, scratch, self._render(bundle, scratch))
            return asyncio.run(self._within_timeout(prompt, options, bundle.timeout_s))
        finally:
            # Every exit sweeps: a timeout or a dead transport must not leave a
            # rendered page of somebody's invoice on the disk.
            if scratch is not None and root is not None:
                self.last_scratch = close_scratch(scratch)
                log_scratch(root, bundle.job_type, self.last_scratch)

    def _render(
        self, bundle: PromptBundle, scratch: Path | None
    ) -> dict[Path, tuple[RenderedPages, tuple[Path, ...]]]:
        """Page images for every attachment that has no text layer, written
        into this call's own directory so they are swept with it.

        Nothing here can fail a call: a document the renderer cannot open is
        simply not in the mapping, and :func:`read_lines` then names the file
        the way it always did.
        """
        if scratch is None:
            return {}
        rendered: dict[Path, tuple[RenderedPages, tuple[Path, ...]]] = {}
        for attachment in bundle.attachments:
            pages = render_attachment(attachment)
            if pages is None:
                continue
            try:
                written = write_pages(pages, scratch / PAGES_DIRNAME)
            except OSError:
                continue
            rendered[attachment.path] = (pages, written)
        return rendered

    # -- the call ----------------------------------------------------------------

    def _options(self, mod: Any, bundle: PromptBundle, scratch: Path | None = None):
        model = bundle.model
        readable = readable_paths(bundle, scratch)

        async def pre_tool_use(input_data, tool_use_id, context):
            name = str(input_data.get("tool_name", ""))
            tool_input = dict(input_data.get("tool_input") or {})
            reason = read_refusal(name, tool_input, readable, scratch)
            if reason is None:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        return mod.ClaudeAgentOptions(
            allowed_tools=list(SESSION_TOOLS) if bundle.attachments else [],
            disallowed_tools=list(DENIED_TOOLS),
            max_turns=self._max_turns,
            max_buffer_size=self._max_buffer_size,
            setting_sources=[],
            hooks={"PreToolUse": [mod.HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            **({"cwd": str(scratch)} if scratch is not None else {}),
            **({"model": model} if model and model != DEFAULT_MODEL else {}),
        )

    async def _within_timeout(self, prompt: str, options: Any, timeout_s: int) -> RawReply:
        # wait_for cancels _drain on timeout; the cancellation propagates into
        # the SDK async generator so its transport subprocess is torn down
        # rather than left running (docs/lessons.md, "A declared bound is an enforced bound").
        return await asyncio.wait_for(self._drain(prompt, options), timeout_s)

    async def _drain(self, prompt: str, options: Any) -> RawReply:
        chunks: list[str] = []
        tokens: tuple[int, int] | None = None
        model: str | None = None
        messages = stream(prompt, options)
        try:
            async for message in messages:
                result = getattr(message, "result", None)
                if isinstance(result, str):
                    chunks.append(result)
                reported = _usage(message)
                if reported is not None:
                    tokens = reported  # the last report wins (it is cumulative)
                model = getattr(message, "model", None) or model
        finally:
            aclose = getattr(messages, "aclose", None)
            if aclose is not None:
                await aclose()
        usage = Usage(*tokens) if tokens else Usage()
        return RawReply(text="\n".join(chunks), usage=usage, model=model)
