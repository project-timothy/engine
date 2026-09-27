"""The Claude Agent SDK as a ``complete()`` adapter (row 7.10's dependency).

Before this row, ``claude_agent_sdk`` was a describe-only tier name: the
policy refused to build it, so a tenant could say "the seat is what I run"
but no job could be pointed through the policy at that seat. The AP
extractor could therefore not move behind the gateway without changing what
the owner pays. This adapter wraps the same SDK call ``ClaudeExtractor``
made, so the seat serves ``invoice_extract`` at the same zero metered cost.

Every test stands in for the SDK: nothing here spawns the Claude Code binary.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from conftest import image_only_pdf, minimal_pdf
from core.engine.config import TenantConfig
from core.llm import Attachment, GatewayTransportError, Message, PromptBundle
from core.llm.adapters.claude_sdk_complete import (
    BASH_TOOL,
    DATA_ROOT_ENV,
    DEFAULT_MODEL,
    DENIED_TOOLS,
    LOCAL_SCRATCH_DIRNAME,
    MAX_TURNS,
    READ_TOOL,
    SCRATCH_DIRNAME,
    SCRATCH_LABEL,
    SCRATCH_LOG,
    SCRATCH_ROOT_ENV,
    SDK_BUFFER_BYTES,
    SESSION_TOOLS,
    ClaudeSdkCompleteAdapter,
    build_prompt,
    resolve_scratch_root,
)
from core.llm.policy import build_adapter, resolve
from core.llm.rasterize import PAGES_DIRNAME
from core.llm.sdk import INSTALL_HINT, SdkMissing

REPLY = '{"doc_type": "invoice", "confidence": 0.9}'


class _Options:
    """Stands in for ``ClaudeAgentOptions``: keeps whatever it was handed."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Result:
    def __init__(self, text: str, usage: dict | None = None, model: str | None = None) -> None:
        self.result = text
        self.usage = usage or {}
        self.model = model


class _FakeSdk:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.ClaudeAgentOptions = _Options

    def query(self, *, prompt: str, options: Any):
        self.prompt = prompt
        self.options = options

        async def gen():
            for message in self.messages:
                yield message

        return gen()


def _bundle(
    *,
    messages: tuple[Message, ...] = (
        Message("system", "Classify documents. A credit memo is an invoice."),
        Message("user", "Here is the document text."),
    ),
    attachments: tuple[Attachment, ...] = (),
    model: str = "seat-model",
    timeout_s: int = 120,
) -> PromptBundle:
    return PromptBundle("invoice_extract", model, messages, attachments, timeout_s)


def _wire(monkeypatch, fake: _FakeSdk) -> None:
    from core.llm.adapters import claude_sdk_complete as mod

    monkeypatch.setattr(mod, "sdk", lambda: fake)
    monkeypatch.setattr(
        mod, "stream", lambda prompt, options: fake.query(prompt=prompt, options=options)
    )


# ---- the prompt and the options ----------------------------------------------


def test_the_bundle_becomes_one_prompt_carrying_the_system_text_and_the_turns(monkeypatch):
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)

    reply = ClaudeSdkCompleteAdapter().complete(_bundle(), {"type": "object"})

    assert reply.text == REPLY
    assert "A credit memo is an invoice." in fake.prompt
    assert "Here is the document text." in fake.prompt


def test_an_attachment_turns_on_the_tools_and_names_the_path(monkeypatch, tmp_path):
    """Retargeted twice, intent preserved both times. #265 made this list read
    ``["Read", "Bash"]``, because the live 2026-09-16 sessions ran Bash to
    rasterize a scan and ``allowed_tools`` pre-approves rather than restricts.
    Issue #296 took the reason away: the ENGINE renders now, so the list is
    back to ``["Read"]`` and Bash is refused outright (below). The intent under
    test has not moved: an attachment turns the document-reading tool on and
    names the path."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")

    ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch").complete(
        _bundle(attachments=(Attachment(doc, "application/pdf"),)), {}
    )

    assert str(doc) in fake.prompt
    assert fake.options.kwargs["allowed_tools"] == ["Read"]


def test_no_attachment_means_no_tools_and_no_scratch_directory(monkeypatch, tmp_path):
    """Nothing to read, nothing to render: no tools are pre-approved and no
    directory is made for a session that has no file to work on."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    root = tmp_path / "scratch"

    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)
    adapter.complete(_bundle(), {})

    assert fake.options.kwargs["allowed_tools"] == []
    assert adapter.last_scratch is None
    assert not root.exists()
    assert SCRATCH_LABEL not in fake.prompt


def test_the_buffer_and_the_turn_bound_reach_the_options(monkeypatch):
    """PR #184's lesson: the CLI transport buffer must clear the file cap with
    base64 headroom. The turn bound is the extractor's old setting raised to a
    measured one (docs/lessons.md, "Budget for the hardest input").

    Retargeted (intent preserved) for row 7.11, which moved the receipt-inbox
    classifier and the scan grouper onto this adapter: the 2026-09-04
    incident's two settings were per-site (32 MB buffer and ``max_turns=6``
    on the classifier, ``max_turns=4`` on the grouper) and are the adapter's
    now, so this one test carries all three sites' half of that incident. The
    buffer is the same 32 MB; the turn bound is higher than every site's old
    value, so no site lost headroom in the move."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    ClaudeSdkCompleteAdapter().complete(_bundle(), {})
    assert fake.options.kwargs["max_buffer_size"] == SDK_BUFFER_BYTES == 32 * 1024 * 1024
    assert fake.options.kwargs["max_turns"] == MAX_TURNS
    assert MAX_TURNS > 4, "4 turns cannot read a multi-page scan (2026-09-15)"
    assert MAX_TURNS >= 6, "the inbox classifier needed 6 turns (2026-09-04)"


def test_the_default_model_sentinel_lets_the_cli_choose(monkeypatch):
    """The live tenant's seat tier says ``model = "default"``: the seat runs
    whatever the CLI is logged in as, which is how the extractor called it."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    ClaudeSdkCompleteAdapter().complete(_bundle(model=DEFAULT_MODEL), {})
    assert "model" not in fake.options.kwargs


def test_a_named_model_is_passed_through(monkeypatch):
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    ClaudeSdkCompleteAdapter().complete(_bundle(model="a-named-model"), {})
    assert fake.options.kwargs["model"] == "a-named-model"


# ---- the reply ---------------------------------------------------------------


def test_usage_and_the_provider_model_come_back_on_the_raw_reply(monkeypatch):
    fake = _FakeSdk(
        [_Result(REPLY, usage={"input_tokens": 1200, "output_tokens": 80}, model="seat-resolved")]
    )
    _wire(monkeypatch, fake)

    reply = ClaudeSdkCompleteAdapter().complete(_bundle(), {})

    assert reply.usage.input_tokens == 1200
    assert reply.usage.output_tokens == 80
    assert reply.model == "seat-resolved"


def test_a_seat_that_reports_no_usage_is_zero_not_a_crash(monkeypatch):
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    reply = ClaudeSdkCompleteAdapter().complete(_bundle(), {})
    assert reply.usage.input_tokens == 0
    assert reply.usage.output_tokens == 0


def test_every_result_chunk_joins_into_one_reply(monkeypatch):
    fake = _FakeSdk([_Result('{"doc_type":'), _Result(' "invoice"}')])
    _wire(monkeypatch, fake)
    assert ClaudeSdkCompleteAdapter().complete(_bundle(), {}).text.replace("\n", "") == (
        '{"doc_type": "invoice"}'
    )


# ---- the failures ------------------------------------------------------------


def test_a_missing_sdk_is_a_terminal_transport_failure_naming_the_extra(monkeypatch):
    from core.llm.adapters import claude_sdk_complete as mod

    def missing():
        raise SdkMissing("the Claude Agent SDK gateway adapter")

    monkeypatch.setattr(mod, "sdk", missing)
    with pytest.raises(GatewayTransportError) as caught:
        ClaudeSdkCompleteAdapter().complete(_bundle(), {})

    assert caught.value.cause == "sdk_missing"
    assert caught.value.transient is False, "no redial installs a package"
    assert INSTALL_HINT in str(caught.value)


def test_a_stalled_transport_times_out_instead_of_hanging(monkeypatch):
    """The 2026-06-19 incident shape, now owned by the adapter: a wedged SDK
    transport fails this one call fast."""
    from core.llm.adapters import claude_sdk_complete as mod

    fake = _FakeSdk([])
    monkeypatch.setattr(mod, "sdk", lambda: fake)

    def never_answers(prompt, options):
        async def gen():
            await asyncio.sleep(30)
            yield _Result(REPLY)

        return gen()

    monkeypatch.setattr(mod, "stream", never_answers)
    with pytest.raises(TimeoutError):
        ClaudeSdkCompleteAdapter().complete(_bundle(timeout_s=1), {})


# ---- the policy now builds it ------------------------------------------------


def test_the_policy_builds_the_sdk_adapter_instead_of_refusing_it():
    cfg = TenantConfig.model_validate(
        {
            "identity": {"legal_name": "Seat Co", "slug": "demo"},
            "llm": {
                "tiers": {
                    "seat": {
                        "adapter": "claude_agent_sdk",
                        "model": DEFAULT_MODEL,
                        "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
                    }
                },
                "jobs": {"invoice_extract": "seat"},
            },
        }
    )
    resolved = resolve(cfg.llm, "invoice_extract")
    assert resolved.adapter == "claude_agent_sdk"
    adapter = build_adapter(resolved)
    assert adapter.name == "claude_agent_sdk"
    assert isinstance(adapter, ClaudeSdkCompleteAdapter)
    # The seat is flat rate: a call through it prices at zero.
    assert resolved.pricing.input_usd_per_mtok == Decimal("0")


def test_the_adapter_module_names_no_model_family():
    from core.llm.adapters import claude_sdk_complete as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for family in ("opus", "sonnet", "haiku", "gpt-", "qwen", "gemini"):
        assert family not in source.lower(), f"{family} is tenant policy, not adapter code"


# ---- the scratch directory (issue #265) --------------------------------------
#
# The session is NOT Read-only, and it never was: `allowed_tools` pre-approves
# in the Claude Agent SDK, it does not restrict. The live 2026-09-16 sessions
# ran `pdftoppm` through Bash into a shared temporary directory nobody owned,
# then read the pages back. The owner's call on 2026-09-17 was to keep Bash and
# give the session a named directory of the engine's own, said out loud in the
# design note and the row's decision file.

SHARED_TEMP = "/tmp"
"""What the live sessions wrote into, and what nothing in the adapter may name
again. Spelled here (a test file may name it) and nowhere in the module."""


def _scratch_from(prompt: str) -> Path:
    line = next(line for line in prompt.splitlines() if line.startswith(SCRATCH_LABEL))
    return Path(line[len(SCRATCH_LABEL) :].strip())


class _WritingSdk(_FakeSdk):
    """A session that writes a file the way the live one did: it reads the
    scratch directory out of its own prompt and renders a page into it."""

    def __init__(self, messages: list[Any], *, name: str, data: bytes) -> None:
        super().__init__(messages)
        self._name = name
        self._data = data

    def query(self, *, prompt: str, options: Any):
        target = _scratch_from(prompt) / self._name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._data)
        return super().query(prompt=prompt, options=options)


class _BashReachingSdk(_FakeSdk):
    """A session that reaches for Bash the way the live ones did. The real SDK
    removes a disallowed tool from the model's context, so the stand-in asks
    the same question of the options it was handed: a tool has to be
    pre-approved AND not refused before anything runs."""

    def __init__(self, messages: list[Any]) -> None:
        super().__init__(messages)
        self.refused: list[str] = []

    def query(self, *, prompt: str, options: Any):
        allowed = options.kwargs.get("allowed_tools", [])
        denied = options.kwargs.get("disallowed_tools", [])
        if "Bash" in allowed and "Bash" not in denied:
            # What it would have done: render its own page beside the engine's.
            (_scratch_from(prompt) / "rendered-by-the-model.png").write_bytes(b"png")
        else:
            self.refused.append("Bash")
        return super().query(prompt=prompt, options=options)


def test_the_prompt_names_the_scratch_directory_and_no_shared_temporary_one():
    """The prompt is the only place the session hears where it may write, so
    the path has to be in it, and the shared temporary directory must not be."""
    scratch = Path("/engine-state/llm-scratch/invoice_extract-20260917T120000-abcd1234")
    prompt = build_prompt(
        _bundle(attachments=(Attachment(Path("/documents/invoice.pdf"), "application/pdf"),)),
        scratch=scratch,
    )

    assert str(scratch) in prompt
    assert SHARED_TEMP not in prompt
    assert "/documents/invoice.pdf" in prompt, "the attachment line still names the document"


def test_the_scratch_directory_exists_during_the_call_and_is_gone_after(monkeypatch, tmp_path):
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    root = tmp_path / "scratch"

    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)
    adapter.complete(_bundle(attachments=(Attachment(doc, "application/pdf"),)), {})

    scratch = _scratch_from(fake.prompt)
    assert scratch.parent == root, "one directory per call, under the named root"
    assert adapter.last_scratch is not None
    assert adapter.last_scratch.path == scratch
    assert adapter.last_scratch.removed is True
    assert not scratch.exists()


def test_what_the_session_wrote_is_inventoried_and_recorded_before_it_is_removed(
    monkeypatch, tmp_path
):
    """The transcript has to stay auditable after the directory is gone: the
    record names the path, what was left in it, and how many bytes."""
    fake = _WritingSdk([_Result(REPLY)], name="pages/page-1.png", data=b"PNG-bytes-01")
    _wire(monkeypatch, fake)
    doc = tmp_path / "scan.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    root = tmp_path / "scratch"

    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)
    adapter.complete(_bundle(attachments=(Attachment(doc, "application/pdf"),)), {})

    record = adapter.last_scratch
    assert record is not None
    assert record.files == (("pages/page-1.png", 12),)
    assert record.total_bytes == 12
    assert not record.path.exists()

    rows = [
        json.loads(line)
        for line in (root / SCRATCH_LOG).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["dir"] == str(record.path)
    assert rows[0]["job_type"] == "invoice_extract"
    assert rows[0]["files"] == [{"path": "pages/page-1.png", "bytes": 12}]
    assert rows[0]["bytes"] == 12
    assert rows[0]["removed"] is True
    assert rows[0]["at"]


def test_a_failed_call_still_sweeps_its_directory_and_records_it(monkeypatch, tmp_path):
    """A transport failure must not leave a rendered document on the disk."""
    from core.llm.adapters import claude_sdk_complete as mod

    fake = _FakeSdk([])
    monkeypatch.setattr(mod, "sdk", lambda: fake)
    prompts: list[str] = []

    def explodes(prompt, options):
        prompts.append(prompt)
        scratch = _scratch_from(prompt)
        (scratch / "page-1.png").write_bytes(b"half a render")

        async def gen():
            raise RuntimeError("the transport died")
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(mod, "stream", explodes)
    doc = tmp_path / "scan.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    root = tmp_path / "scratch"
    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)

    with pytest.raises(RuntimeError):
        adapter.complete(_bundle(attachments=(Attachment(doc, "application/pdf"),)), {})

    scratch = _scratch_from(prompts[0])
    assert not scratch.exists()
    assert adapter.last_scratch is not None
    assert adapter.last_scratch.files == (("page-1.png", 13),)
    assert (root / SCRATCH_LOG).is_file()


def test_two_calls_never_share_a_directory(monkeypatch, tmp_path):
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    root = tmp_path / "scratch"
    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)
    bundle = _bundle(attachments=(Attachment(doc, "application/pdf"),))

    adapter.complete(bundle, {})
    first = adapter.last_scratch.path
    adapter.complete(bundle, {})
    second = adapter.last_scratch.path

    assert first != second
    assert first.name.startswith("invoice_extract-"), "the directory names the job it served"


def test_the_scratch_root_follows_the_engine_state_conventions(tmp_path, monkeypatch):
    """Same shape as the ledger root and the auditor store: an explicit value
    wins, then this root's own variable, then the container's data volume,
    then a directory beside the run."""
    explicit = tmp_path / "given"
    assert resolve_scratch_root(explicit, env={SCRATCH_ROOT_ENV: "/ignored"}) == explicit
    assert resolve_scratch_root(env={SCRATCH_ROOT_ENV: str(tmp_path / "named")}) == (
        tmp_path / "named"
    )
    assert resolve_scratch_root(env={DATA_ROOT_ENV: "/data"}) == Path("/data") / SCRATCH_DIRNAME

    monkeypatch.chdir(tmp_path)
    assert resolve_scratch_root(env={}) == tmp_path / LOCAL_SCRATCH_DIRNAME


def test_the_adapter_module_never_names_a_shared_temporary_directory():
    from core.llm.adapters import claude_sdk_complete as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert SHARED_TEMP not in source, "the session writes to the engine's own directory"


def test_a_root_that_cannot_hold_a_directory_is_a_terminal_transport_failure(monkeypatch, tmp_path):
    """If there is no writable place the session may use, the call fails
    naming that, rather than running a session with no named destination."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file sits where the root should be", encoding="utf-8")

    with pytest.raises(GatewayTransportError) as caught:
        ClaudeSdkCompleteAdapter(scratch_root=blocked).complete(
            _bundle(attachments=(Attachment(doc, "application/pdf"),)), {}
        )

    assert caught.value.cause == "scratch_unavailable"
    assert caught.value.transient is False, "a redial does not make a root writable"
    assert SCRATCH_ROOT_ENV in str(caught.value)


# ---- the engine renders, so the session is Read-only (issue #296) -------------
#
# #265 kept Bash because rasterizing an image-only PDF was the only way that
# class of document extracted at all. This row moved the rendering into the
# engine (`core/llm/rasterize.py`), so the session no longer has anything to
# shell out FOR: it gets page images to read, `allowed_tools` shrinks to
# `Read`, and `disallowed_tools` refuses the rest rather than merely leaving
# them unapproved.


def _scan(tmp_path: Path, pages: int = 1, name: str = "scan.pdf") -> Attachment:
    path = tmp_path / name
    path.write_bytes(image_only_pdf(pages))
    return Attachment(path, "application/pdf")


def _read_lines(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines() if line.startswith("Read ")]


def test_an_image_only_pdf_reaches_the_session_as_rendered_pages_not_as_the_pdf(
    monkeypatch, tmp_path
):
    """The acceptance clause: the prompt names one rendered page image per
    page, and the pages sit in the call's own scratch directory."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    scan = _scan(tmp_path, pages=2)
    root = tmp_path / "scratch"

    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)
    adapter.complete(_bundle(attachments=(scan,)), {})

    scratch = _scratch_from(fake.prompt)
    pages = [scratch / PAGES_DIRNAME / f"page-{n}.png" for n in (1, 2)]
    assert _read_lines(fake.prompt) == [f"Read the page image at {p}" for p in pages]
    assert str(scan.path) not in fake.prompt, "the session is not sent to the unreadable PDF"
    assert scan.path.name in fake.prompt, "the note still says which document it came from"


def test_the_rendered_pages_are_inventoried_and_swept_with_the_directory(monkeypatch, tmp_path):
    """They are copies of a document the filing tree already holds, so what
    survives the call is the inventory, exactly as it does for anything a
    session writes (#265)."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    root = tmp_path / "scratch"

    adapter = ClaudeSdkCompleteAdapter(scratch_root=root)
    adapter.complete(_bundle(attachments=(_scan(tmp_path, pages=2),)), {})

    record = adapter.last_scratch
    assert record is not None
    assert [name for name, _size in record.files] == ["pages/page-1.png", "pages/page-2.png"]
    assert all(size > 0 for _name, size in record.files)
    assert record.removed is True
    assert not record.path.exists()


def test_a_pdf_with_a_text_layer_is_still_handed_over_as_the_file_itself(monkeypatch, tmp_path):
    """Nothing renders when the model can read the document: the unchanged
    path stays unchanged."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(minimal_pdf("ACME LLC\nINVOICE 4471\nTOTAL 1875.00"))

    adapter = ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch")
    adapter.complete(_bundle(attachments=(Attachment(doc, "application/pdf"),)), {})

    assert _read_lines(fake.prompt) == [f"Read the document at {doc}"]
    assert adapter.last_scratch is not None
    assert adapter.last_scratch.files == (), "nothing was rendered, so nothing was written"


def test_a_pdf_that_cannot_be_rendered_still_reaches_the_model(monkeypatch, tmp_path):
    """The eval the issue asks for by name: a broken file falls back to the
    attachment it always was instead of failing the extraction."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)
    broken = tmp_path / "half-a-scan.pdf"
    broken.write_bytes(b"%PDF-1.4 truncated before anything useful")

    ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch").complete(
        _bundle(attachments=(Attachment(broken, "application/pdf"),)), {}
    )

    assert _read_lines(fake.prompt) == [f"Read the document at {broken}"]


def test_the_session_is_pre_approved_for_read_alone(monkeypatch, tmp_path):
    """Retargeted from #265 (intent preserved): the pre-approval list said
    ``["Read", "Bash"]`` because the session rendered its own pages. It no
    longer does, so the list is back to the one tool a document needs."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)

    ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch").complete(
        _bundle(attachments=(_scan(tmp_path),)), {}
    )

    assert fake.options.kwargs["allowed_tools"] == ["Read"]
    assert SESSION_TOOLS == (READ_TOOL,)
    assert BASH_TOOL not in SESSION_TOOLS


def test_bash_is_refused_rather_than_left_unapproved(monkeypatch, tmp_path):
    """The whole point of #265 was that an unlisted tool is not a refused one.
    ``disallowed_tools`` is the SDK option that removes a tool from the
    session's context outright, so the confinement is stated, not implied."""
    fake = _FakeSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)

    ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch").complete(
        _bundle(attachments=(_scan(tmp_path),)), {}
    )

    denied = fake.options.kwargs["disallowed_tools"]
    assert BASH_TOOL in denied
    assert denied == list(DENIED_TOOLS)
    assert not set(denied) & set(fake.options.kwargs["allowed_tools"]), "no tool is both"


def test_a_session_that_reaches_for_bash_finds_it_refused(monkeypatch, tmp_path):
    """The fixture the issue asks for: a session that tries to shell out the
    way the live 2026-09-16 ones did gets nowhere, and the only files in the
    scratch directory are the ones the ENGINE rendered."""
    fake = _BashReachingSdk([_Result(REPLY)])
    _wire(monkeypatch, fake)

    adapter = ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch")
    adapter.complete(_bundle(attachments=(_scan(tmp_path),)), {})

    assert fake.refused == [BASH_TOOL]
    assert adapter.last_scratch is not None
    assert [name for name, _size in adapter.last_scratch.files] == ["pages/page-1.png"]


def test_the_denied_tools_cover_writing_and_running_not_just_bash():
    """A denylist is what the SDK offers, so it has to name every tool that
    could write to this host or run something on it, not only the one the live
    sessions happened to use."""
    for tool in ("Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task"):
        assert tool in DENIED_TOOLS, f"{tool} is neither reading nor refused"
