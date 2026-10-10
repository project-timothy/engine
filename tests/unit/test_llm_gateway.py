"""The model gateway (phase 7 row 7.8, docs/model-seam-design.md).

One ``complete()`` call, one validated pydantic instance back, one
``CallRecord`` of what it cost. Every adapter is exercised here with no
network: the fixture adapter answers from canned replies, the Anthropic and
OpenAI-compatible adapters run against recorded request and response bodies
with their transport monkeypatched. The suite also pins the two rules the
seam exists for: money is a Decimal STRING (never a float), and no model name
is hard-coded under core/llm (the tenant policy names models, row 7.9).
"""

from __future__ import annotations

import base64
import io
import json
import tokenize
import urllib.error
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from conftest import image_only_pdf, minimal_pdf
from core.llm import (
    Attachment,
    CallRecord,
    DecimalString,
    GatewayResult,
    GatewaySchemaError,
    GatewayTransportError,
    GatewayValidationError,
    Message,
    Pricing,
    PromptBundle,
    RawReply,
    Usage,
    complete,
)
from core.llm.adapters import anthropic_messages, openai_compat
from core.llm.adapters.anthropic_messages import AnthropicMessagesAdapter
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.adapters.openai_compat import OpenAICompatAdapter
from core.llm.rasterize import PAGE_MIME, PNG_MAGIC

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
CORE_LLM = Path(__file__).resolve().parents[2] / "core" / "llm"

GOOD_REPLY = '{"vendor": "Acme Fasteners", "amount": "1875.00", "confidence": 0.91}'
BAD_REPLY_MISSING = '{"vendor": "Acme Fasteners", "confidence": 0.91}'
BAD_REPLY_PROSE = "Sure! Here is the answer you asked for."
FLOAT_MONEY_REPLY = '{"vendor": "Acme Fasteners", "amount": 1875.0, "confidence": 0.91}'


class InvoiceLine(BaseModel):
    vendor: str
    amount: DecimalString
    confidence: float = Field(ge=0.0, le=1.0)


class BadMoneyModel(BaseModel):
    vendor: str
    amount: Decimal


def _messages() -> list[Message]:
    return [
        Message(role="system", content="You classify documents for an intake system."),
        Message(role="user", content="Extract the vendor and total from the attached invoice."),
    ]


def _call(adapter, *, output_model=InvoiceLine, **kw) -> GatewayResult:
    return complete(
        "invoice_extract",
        _messages(),
        output_model,
        adapter=adapter,
        model="policy-chosen-model",
        **kw,
    )


# ---- the fixture adapter and the validation contract ---------------------


def test_fixture_adapter_round_trips_a_decimal_string_money_field():
    adapter = FixtureAdapter({"invoice_extract": GOOD_REPLY}, usage=Usage(120, 20))
    result = _call(adapter)
    assert isinstance(result.output, InvoiceLine)
    assert result.output.amount == "1875.00"  # exactly the string given, no float round trip
    assert Decimal(result.output.amount) == Decimal("1875.00")  # the caller re-parses in code
    assert isinstance(result.record, CallRecord)
    assert result.record.job_type == "invoice_extract"
    assert result.record.adapter == "fixture"
    assert result.record.model == "policy-chosen-model"
    assert result.record.input_tokens == 120
    assert result.record.output_tokens == 20
    assert result.record.retries == 0
    assert result.record.usd is None  # no pricing given, no number invented
    assert result.record.latency_ms >= 0


def test_the_schema_handed_to_the_adapter_is_the_models_json_schema():
    seen: list[dict] = []

    def reply(bundle: PromptBundle, schema: dict) -> RawReply:
        seen.append(schema)
        return RawReply(text=GOOD_REPLY, usage=Usage(1, 1))

    _call(FixtureAdapter(reply))
    assert seen == [InvoiceLine.model_json_schema()]


def test_a_float_for_money_never_validates():
    adapter = FixtureAdapter([FLOAT_MONEY_REPLY, FLOAT_MONEY_REPLY])
    with pytest.raises(GatewayValidationError):
        _call(adapter)


def test_a_decimal_typed_output_field_is_refused_before_any_call():
    adapter = FixtureAdapter([GOOD_REPLY])
    with pytest.raises(GatewaySchemaError, match="amount"):
        _call(adapter, output_model=BadMoneyModel)
    assert adapter.calls == []


def test_a_malformed_first_reply_and_a_valid_second_reply_succeed_with_one_retry():
    adapter = FixtureAdapter([BAD_REPLY_MISSING, GOOD_REPLY])
    result = _call(adapter)
    assert result.output.amount == "1875.00"
    assert result.record.retries == 1
    assert len(adapter.calls) == 2
    first, second = adapter.calls
    # The retry carries the failed reply back as the assistant turn and the
    # validation error in the follow-up user turn, so the model sees what it
    # got wrong. The original messages are untouched.
    assert [m.content for m in second.messages[: len(first.messages)]] == [
        m.content for m in first.messages
    ]
    assert second.messages[-2].role == "assistant"
    assert second.messages[-2].content == BAD_REPLY_MISSING
    assert second.messages[-1].role == "user"
    assert "amount" in second.messages[-1].content
    assert "validation error" in second.messages[-1].content


def test_prose_around_the_json_object_is_tolerated():
    adapter = FixtureAdapter(["Here you go:\n```json\n" + GOOD_REPLY + "\n```\nDone."])
    assert _call(adapter).output.vendor == "Acme Fasteners"


def test_two_malformed_replies_raise_carrying_both_raw_replies():
    adapter = FixtureAdapter([BAD_REPLY_PROSE, BAD_REPLY_MISSING])
    with pytest.raises(GatewayValidationError) as info:
        _call(adapter)
    exc = info.value
    assert exc.job_type == "invoice_extract"
    assert exc.replies == [BAD_REPLY_PROSE, BAD_REPLY_MISSING]
    assert len(exc.errors) == 2
    assert "invoice_extract" in str(exc)
    assert len(adapter.calls) == 2


def test_a_transport_exception_maps_to_gateway_transport_error_without_retry():
    adapter = FixtureAdapter(ConnectionError("gateway unreachable"))
    with pytest.raises(GatewayTransportError) as info:
        _call(adapter)
    assert info.value.cause == "transport_error"
    assert info.value.transient is True
    assert "gateway unreachable" in str(info.value)
    assert len(adapter.calls) == 1


def test_a_timeout_maps_to_a_timeout_cause():
    adapter = FixtureAdapter(TimeoutError("read timed out"))
    with pytest.raises(GatewayTransportError) as info:
        _call(adapter)
    assert info.value.cause == "timeout"
    assert info.value.transient is True


def test_usd_is_computed_from_pricing_as_decimal():
    adapter = FixtureAdapter([GOOD_REPLY], usage=Usage(1000, 500))
    pricing = Pricing(input_usd_per_mtok=Decimal("3"), output_usd_per_mtok=Decimal("15"))
    result = _call(adapter, pricing=pricing)
    assert result.record.usd == Decimal("0.0105")
    assert isinstance(result.record.usd, Decimal)


def test_fixture_adapter_by_job_type_falls_back_to_default():
    adapter = FixtureAdapter({"invoice_extract": GOOD_REPLY})
    with pytest.raises(GatewayTransportError, match="no fixture reply"):
        complete(
            "other_job",
            _messages(),
            InvoiceLine,
            adapter=adapter,
            model="policy-chosen-model",
        )


# ---- the Anthropic Messages adapter against a recorded exchange ------------


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _png(tmp_path: Path) -> Path:
    path = tmp_path / "invoice.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    return path


def test_anthropic_adapter_sends_schema_messages_attachments_and_parses_usage(
    tmp_path, monkeypatch
):
    recorded = (FIXTURES / "anthropic_messages_response.json").read_bytes()
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append((request, timeout))
        return _FakeHTTPResponse(recorded)

    monkeypatch.setattr(anthropic_messages, "urlopen", fake_urlopen)
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    image = _png(tmp_path)

    adapter = AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY", max_tokens=2048)
    result = _call(adapter, attachments=[Attachment(path=image, mime="image/png")], timeout_s=33)

    assert result.output.amount == "1875.00"
    assert result.record.adapter == "anthropic_messages"
    assert result.record.input_tokens == 321
    assert result.record.output_tokens == 45
    assert result.record.retries == 0

    (request, timeout), *_ = seen
    assert timeout == 33
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["x-api-key"] == "sk-test-not-a-real-key"
    assert "anthropic-version" in headers
    assert headers["content-type"] == "application/json"

    body = json.loads(request.data)
    assert body["model"] == "policy-chosen-model"
    assert body["max_tokens"] == 2048
    # Current Opus and Sonnet reject any non-default temperature with a 400.
    assert "temperature" not in body
    # The system turn travels in the top-level system field, not as a message.
    assert "classify documents" in body["system"]
    assert [m["role"] for m in body["messages"]] == ["user"]
    blocks = body["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(image.read_bytes()).decode("ascii"),
    }
    assert blocks[-1] == {"type": "text", "text": _messages()[1].content}
    # Structured output: the schema rides output_config.format, tightened to
    # what constrained decoding accepts (every object closed, no numeric
    # bounds; pydantic still enforces the bounds on the way back).
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"vendor", "amount", "confidence"}
    assert "minimum" not in schema["properties"]["confidence"]
    assert "maximum" not in schema["properties"]["confidence"]
    assert InvoiceLine.model_json_schema()["properties"]["confidence"]["minimum"] == 0.0


def test_anthropic_adapter_sends_a_pdf_as_a_document_block(tmp_path, monkeypatch):
    recorded = (FIXTURES / "anthropic_messages_response.json").read_bytes()
    seen: list = []
    monkeypatch.setattr(
        anthropic_messages,
        "urlopen",
        lambda request, timeout=None: seen.append(request) or _FakeHTTPResponse(recorded),
    )
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    adapter = AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY")
    _call(adapter, attachments=[Attachment(path=pdf, mime="application/pdf")])
    block = json.loads(seen[0].data)["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"


def test_anthropic_adapter_sends_an_image_only_pdf_as_rendered_pages(tmp_path, monkeypatch):
    """Issue #296, the half that is not about the seat: a scan has nothing for
    a provider to read out of the PDF, so the ENGINE renders it and every
    adapter sends pictures. Before this row the API adapters could not extract
    a scanned invoice at all, which is the phase 9 blocker."""
    recorded = (FIXTURES / "anthropic_messages_response.json").read_bytes()
    seen: list = []
    monkeypatch.setattr(
        anthropic_messages,
        "urlopen",
        lambda request, timeout=None: seen.append(request) or _FakeHTTPResponse(recorded),
    )
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(image_only_pdf(2))

    adapter = AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY")
    _call(adapter, attachments=[Attachment(path=scan, mime="application/pdf")])

    blocks = json.loads(seen[0].data)["messages"][0]["content"]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 2, "one image block per rendered page"
    assert not [b for b in blocks if b["type"] == "document"], "the unreadable PDF stays home"
    for block in images:
        assert block["source"]["media_type"] == PAGE_MIME
        assert base64.b64decode(block["source"]["data"])[:8] == PNG_MAGIC
    note = [b for b in blocks if b["type"] == "text"][0]["text"]
    assert "scan.pdf" in note and "no text layer" in note


def test_anthropic_adapter_still_sends_a_pdf_with_a_text_layer_as_a_document(tmp_path, monkeypatch):
    recorded = (FIXTURES / "anthropic_messages_response.json").read_bytes()
    seen: list = []
    monkeypatch.setattr(
        anthropic_messages,
        "urlopen",
        lambda request, timeout=None: seen.append(request) or _FakeHTTPResponse(recorded),
    )
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(minimal_pdf("ACME LLC\nINVOICE 4471\nTOTAL 1875.00"))

    adapter = AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY")
    _call(adapter, attachments=[Attachment(path=pdf, mime="application/pdf")])

    blocks = json.loads(seen[0].data)["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["document", "text"]


def test_anthropic_adapter_http_error_is_a_transport_error(monkeypatch):
    def failing_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 529, "Overloaded", {}, io.BytesIO(b'{"error":"overloaded"}')
        )

    monkeypatch.setattr(anthropic_messages, "urlopen", failing_urlopen)
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    with pytest.raises(GatewayTransportError) as info:
        _call(AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY"))
    assert info.value.cause == "transport_error"
    assert "529" in str(info.value)


def test_anthropic_adapter_refuses_to_run_without_the_key(monkeypatch):
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    calls: list = []
    monkeypatch.setattr(anthropic_messages, "urlopen", lambda *a, **k: calls.append(a))
    with pytest.raises(GatewayTransportError) as info:
        _call(AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY"))
    assert info.value.cause == "no_api_key"
    assert info.value.transient is False
    assert calls == []


# ---- the OpenAI-compatible adapter against a recorded exchange -------------


class _FakeChatClient:
    """Stands in for openai.OpenAI: records the constructor and every
    chat.completions.create call, returns the recorded response."""

    def __init__(self, recorded: dict, log: list) -> None:
        self._recorded = recorded
        self._log = log
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self._log.append(kwargs)
        r = self._recorded
        return SimpleNamespace(
            model=r["model"],
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=r["choices"][0]["message"]["content"])
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=r["usage"]["prompt_tokens"],
                completion_tokens=r["usage"]["completion_tokens"],
            ),
        )


def _recorded_with(**changes) -> bytes:
    payload = json.loads((FIXTURES / "anthropic_messages_response.json").read_text())
    payload.update(changes)
    return json.dumps(payload).encode("utf-8")


def test_anthropic_adapter_leaves_room_for_thinking_by_default(monkeypatch):
    """Opus 5.5 and Sonnet 5.5 always think, and thinking counts against
    max_tokens; 4096 left an extraction little room past the reasoning."""
    sent: list = []

    def fake_urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return _FakeHTTPResponse(_recorded_with())

    monkeypatch.setattr(anthropic_messages, "urlopen", fake_urlopen)
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    _call(AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY"))
    assert sent[0]["max_tokens"] == 16000


def test_anthropic_adapter_a_reply_cut_off_at_max_tokens_is_refused(monkeypatch):
    """A truncated reply is half a JSON document; it fails here by name
    instead of downstream as a parse error that reads like the model's."""
    monkeypatch.setattr(
        anthropic_messages,
        "urlopen",
        lambda request, timeout=None: _FakeHTTPResponse(_recorded_with(stop_reason="max_tokens")),
    )
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-not-a-real-key")
    with pytest.raises(GatewayTransportError) as info:
        _call(AnthropicMessagesAdapter(api_key_env="TEST_ANTHROPIC_KEY", max_tokens=2048))
    assert info.value.cause == "max_tokens"
    assert info.value.transient is False
    assert "2048" in str(info.value)


def test_openai_compat_adapter_sends_json_mode_schema_and_parses_usage(tmp_path, monkeypatch):
    recorded = json.loads((FIXTURES / "openai_chat_response.json").read_text())
    constructed: list[dict] = []
    calls: list[dict] = []

    def fake_build_client(*, base_url: str, api_key: str):
        constructed.append({"base_url": base_url, "api_key": api_key})
        return _FakeChatClient(recorded, calls)

    monkeypatch.setattr(openai_compat, "build_client", fake_build_client)
    monkeypatch.setenv("TEST_GATEWAY_KEY", "sk-test-gateway")
    image = _png(tmp_path)

    adapter = OpenAICompatAdapter(
        base_url="http://localhost:4000/v1", api_key_env="TEST_GATEWAY_KEY"
    )
    result = _call(adapter, attachments=[Attachment(path=image, mime="image/png")], timeout_s=44)

    assert result.output.amount == "1875.00"
    assert result.record.adapter == "openai_compat"
    assert result.record.input_tokens == 210
    assert result.record.output_tokens == 33

    assert constructed == [{"base_url": "http://localhost:4000/v1", "api_key": "sk-test-gateway"}]
    (kw,) = calls
    assert kw["model"] == "policy-chosen-model"
    assert kw["temperature"] == 0
    assert kw["timeout"] == 44
    assert kw["response_format"] == {"type": "json_object"}
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["system", "user"]
    # JSON mode has no schema slot, so the schema rides the system prompt.
    system_text = kw["messages"][0]["content"]
    assert "classify documents" in system_text
    assert json.dumps(InvoiceLine.model_json_schema(), sort_keys=True) in system_text
    parts = kw["messages"][1]["content"]
    assert parts[0]["type"] == "image_url"
    data = base64.b64encode(image.read_bytes()).decode("ascii")
    assert parts[0]["image_url"]["url"] == f"data:image/png;base64,{data}"
    assert parts[-1] == {"type": "text", "text": _messages()[1].content}


def test_openai_compat_adapter_sends_an_image_only_pdf_as_rendered_pages(tmp_path, monkeypatch):
    """The local tier is the one that needs this most: a small open model has
    no PDF reader at all, so the `file` content part was never going to answer
    for a scan."""
    recorded = json.loads((FIXTURES / "openai_chat_response.json").read_text())
    calls: list[dict] = []
    monkeypatch.setattr(
        openai_compat, "build_client", lambda **kw: _FakeChatClient(recorded, calls)
    )
    monkeypatch.setenv("TEST_GATEWAY_KEY", "sk-test-gateway")
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(image_only_pdf(2))

    adapter = OpenAICompatAdapter(
        base_url="http://localhost:4000/v1", api_key_env="TEST_GATEWAY_KEY"
    )
    _call(adapter, attachments=[Attachment(path=scan, mime="application/pdf")])

    parts = calls[0]["messages"][1]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 2
    assert not [p for p in parts if p["type"] == "file"], "no PDF part for a scan"
    for part in images:
        head, data = part["image_url"]["url"].split(",", 1)
        assert head == f"data:{PAGE_MIME};base64"
        assert base64.b64decode(data)[:8] == PNG_MAGIC
    note = [p for p in parts if p["type"] == "text"][0]["text"]
    assert "scan.pdf" in note and "no text layer" in note
    assert parts[-1] == {"type": "text", "text": _messages()[1].content}, "the ask still ends it"


def test_openai_compat_adapter_missing_key_uses_the_no_auth_placeholder(monkeypatch):
    """The local gateway runs without a key, as QwenExtractor already does."""
    recorded = json.loads((FIXTURES / "openai_chat_response.json").read_text())
    constructed: list[dict] = []
    monkeypatch.setattr(
        openai_compat,
        "build_client",
        lambda *, base_url, api_key: constructed.append({"base_url": base_url, "api_key": api_key})
        or _FakeChatClient(recorded, []),
    )
    monkeypatch.delenv("TEST_GATEWAY_KEY", raising=False)
    _call(OpenAICompatAdapter(base_url="http://localhost:4000/v1", api_key_env="TEST_GATEWAY_KEY"))
    assert constructed[0]["api_key"] == "sk-no-auth"


def test_openai_compat_adapter_transport_failure_maps(monkeypatch):
    class _Boom:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(openai_compat, "build_client", lambda **kw: _Boom())
    with pytest.raises(GatewayTransportError) as info:
        _call(OpenAICompatAdapter(base_url="http://localhost:4000/v1", api_key_env="NOPE"))
    assert info.value.cause == "transport_error"


# ---- the seam's standing rule: no model name under core/llm ---------------


def _code_without_comments(path: Path) -> str:
    out: list[str] = []
    with path.open("rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type != tokenize.COMMENT:
                out.append(tok.string)
    return "\n".join(out)


def test_no_model_name_is_hard_coded_under_core_llm():
    files = sorted(CORE_LLM.rglob("*.py"))
    assert files, "core/llm exists"
    hits: list[str] = []
    for path in files:
        code = _code_without_comments(path).lower()
        for literal in ("claude-", "gpt-", "qwen", "sonnet", "haiku", "opus"):
            if literal in code:
                hits.append(f"{path.relative_to(CORE_LLM)}: {literal!r}")
    assert hits == [], (
        "model names belong in tenant.toml [llm.tiers] (row 7.9), not in core/llm: " + str(hits)
    )
