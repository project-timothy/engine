"""Security review 2026-10-03 (finding 7, MEDIUM): the extraction Read tool.

On the Claude Agent SDK tier a document-extraction session had ``Read``
pre-approved with no working directory, no settings isolation and no hook,
so a document carrying instructions could have the session read the
QuickBooks token file and return it in an extracted field. ``Read`` is now
confined by a PreToolUse hook to the call's own attachments and its scratch
directory, every other tool is denied by the same hook (an allowlist behind
the denylist), and the session loads no settings and runs in its scratch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.llm.adapters.claude_sdk_complete import ClaudeSdkCompleteAdapter
from core.llm.gateway import Attachment, Message, PromptBundle

REPLY = '{"doc_type": "invoice", "confidence": 0.9}'


class _Options:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _HookMatcher:
    def __init__(self, matcher=None, hooks=None) -> None:
        self.matcher = matcher
        self.hooks = hooks or []


class _Result:
    def __init__(self, text: str) -> None:
        self.result = text
        self.usage: dict = {}
        self.model = None


class _FakeSdk:
    ClaudeAgentOptions = _Options
    HookMatcher = _HookMatcher

    def query(self, *, prompt: str, options: Any):
        self.options = options

        async def gen():
            yield _Result(REPLY)

        return gen()


def _complete(monkeypatch, tmp_path: Path, attachments=()):
    from core.llm.adapters import claude_sdk_complete as mod

    fake = _FakeSdk()
    monkeypatch.setattr(mod, "sdk", lambda: fake)
    monkeypatch.setattr(
        mod, "stream", lambda prompt, options: fake.query(prompt=prompt, options=options)
    )
    bundle = PromptBundle(
        "invoice_extract", "seat-model", (Message("user", "extract"),), tuple(attachments), 120
    )
    ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch").complete(bundle, {})
    return fake.options.kwargs


def _decide(kwargs: dict, tool: str, tool_input: dict) -> bool:
    (matcher,) = kwargs["hooks"]["PreToolUse"]
    (hook,) = matcher.hooks
    out = asyncio.run(hook({"tool_name": tool, "tool_input": tool_input}, "t1", None))
    return out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_read_is_confined_to_the_attachment_and_the_scratch(monkeypatch, tmp_path):
    doc = tmp_path / "in" / "invoice.pdf"
    doc.parent.mkdir()
    doc.write_bytes(b"%PDF-1.4 stub")
    secret = tmp_path / "qbo_tokens.json"
    secret.write_text('{"refresh_token": "x"}')

    kwargs = _complete(monkeypatch, tmp_path, [Attachment(doc, "application/pdf")])
    scratch = Path(kwargs["cwd"])

    assert _decide(kwargs, "Read", {"file_path": str(doc)})
    assert _decide(kwargs, "Read", {"file_path": str(scratch / "pages" / "page-1.png")})
    assert not _decide(kwargs, "Read", {"file_path": str(secret)})
    assert not _decide(kwargs, "Read", {"file_path": str(doc.parent / ".." / secret.name)})
    assert not _decide(kwargs, "Read", {"file_path": "../../qbo_tokens.json"})
    assert not _decide(kwargs, "Read", {})


def test_every_other_tool_is_denied_by_the_hook(monkeypatch, tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    kwargs = _complete(monkeypatch, tmp_path, [Attachment(doc, "application/pdf")])
    for tool in ("Bash", "Grep", "Glob", "WebFetch", "Write", "TodoWrite", "mcp__x__y"):
        assert not _decide(kwargs, tool, {"file_path": str(doc), "command": "id"}), tool


def test_the_session_loads_no_settings_and_runs_in_its_scratch(monkeypatch, tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    kwargs = _complete(monkeypatch, tmp_path, [Attachment(doc, "application/pdf")])
    assert kwargs["setting_sources"] == []
    assert Path(kwargs["cwd"]).is_relative_to((tmp_path / "scratch").resolve())


def test_no_attachment_means_nothing_is_readable(monkeypatch, tmp_path):
    kwargs = _complete(monkeypatch, tmp_path)
    assert kwargs["allowed_tools"] == []
    assert not _decide(kwargs, "Read", {"file_path": str(tmp_path / "anything.pdf")})
