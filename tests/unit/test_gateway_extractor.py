"""GatewayExtractor: one LLM boundary, the provider chosen by tenant policy.

Retargeted from ``test_qwen_extractor.py`` in phase 7 row 7.10, intent
preserved. ``QwenExtractor`` was a class holding the local OpenAI-compatible
lane; the lane is now a TIER (``[llm.tiers].local``) and the class is one
:class:`GatewayExtractor`. Every intent the old file pinned still lives here
except the two the row deliberately moved:

- the OpenAI client wiring (base_url, the no-auth placeholder, JSON mode,
  temperature 0) belongs to ``core/llm/adapters/openai_compat.py`` and is
  pinned by that adapter's own tests (row 7.8);
- the ``vision_model`` knob is gone: whether a tier can read an image is a
  property of the tier, so an image-only document rides as an attachment and
  the configured model answers (or says ``needs_ocr`` itself) instead of the
  extractor short-circuiting on a knob.

The contract that stays: an ``ExtractedDocument`` that passes validation, or
an ``ExtractionError`` with a ``cause`` and a ``transient`` flag the retry
wrapper and the ledger can reason about. No network, no gateway, no model:
every call runs against the fixture adapter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents.ap.extraction import (
    ExtractionError,
    GatewayExtractor,
    RetryingExtractor,
)
from core.agents.ap.schema import ExtractedDocument
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger
from core.llm.adapters.fixture import FixtureAdapter

_GOOD_REPLY = json.dumps(
    {
        "doc_type": "invoice",
        "vendor_name": "Arrow Assembly",
        "invoice_number": "INV-2048",
        "amount": "1875.00",
        "invoice_date": "2026-06-01",
        "due_date": "2026-07-01",
        "confidence": 0.94,
        "warnings": [],
        "needs_ocr": False,
    }
)

_TENANT = {
    "identity": {"legal_name": "Gateway Extractor Test Co", "slug": "demo"},
    "llm": {
        "tiers": {
            "local": {
                "adapter": "openai_compat",
                "model": "local-coder",
                "base_url": "http://localhost:4000/v1",
                "api_key_env": "ENGINE_GATEWAY_KEY",
                "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
            }
        },
        "jobs": {"invoice_extract": "local"},
    },
}


def _ctx(tmp_path: Path) -> JobContext:
    return JobContext(
        tenant=TenantConfig.model_validate(_TENANT),
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.k1",
    )


def _doc(tmp_path: Path, name: str = "invoice.pdf", size: int = 64) -> Path:
    f = tmp_path / name
    f.write_bytes(b"x" * size)
    return f


def _build(tmp_path, *, text="ACME invoice, total due $1,875.00", adapter=None, **kw):
    """A GatewayExtractor with an injected text reader and a seeded adapter."""
    adapter = adapter if adapter is not None else FixtureAdapter({"*": _GOOD_REPLY})
    ex = GatewayExtractor(_ctx(tmp_path), adapter=adapter, text_reader=lambda _p: text, **kw)
    return ex, adapter


# ---------- happy path -----------------------------------------------------


def test_valid_reply_validates_to_extracted_document(tmp_path):
    ex, adapter = _build(tmp_path)
    doc = ex.extract(_doc(tmp_path))

    assert isinstance(doc, ExtractedDocument)
    assert doc.doc_type == "invoice"
    assert doc.vendor_name == "Arrow Assembly"
    assert str(doc.amount) == "1875.00"
    assert doc.confidence == 0.94
    assert len(adapter.calls) == 1, "the tier should have been called once"
    assert adapter.calls[0].model == "local-coder", "the model id comes from the tier"


def test_prompt_carries_the_extracted_document_text(tmp_path):
    # A text-only tier classifies nothing unless the text layer is inlined.
    ex, adapter = _build(tmp_path, text="Arrow Assembly  INVOICE  INV-2048  $1,875.00")
    ex.extract(_doc(tmp_path))

    sent = "\n".join(m.content for m in adapter.calls[0].turns())
    assert "Arrow Assembly" in sent
    assert "INV-2048" in sent


def test_json_wrapped_in_prose_or_fences_still_parses(tmp_path):
    noisy = f"Sure, here is the JSON you asked for:\n```json\n{_GOOD_REPLY}\n```"
    ex, _ = _build(tmp_path, adapter=FixtureAdapter({"*": noisy}))
    doc = ex.extract(_doc(tmp_path))
    assert doc.invoice_number == "INV-2048"


# ---------- terminal failures (never retried) ------------------------------


def test_reply_with_no_json_is_terminal_bad_reply(tmp_path):
    # Two replies: the gateway hands the error back once before giving up.
    ex, _ = _build(tmp_path, adapter=FixtureAdapter(["I could not read that.", "Still cannot."]))
    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))
    assert caught.value.cause == "bad_reply"
    assert caught.value.transient is False


def test_json_that_fails_schema_is_terminal_bad_reply(tmp_path):
    # confidence out of the 0..1 range: a malformed reply, not a workaround.
    bad = json.dumps({"doc_type": "invoice", "confidence": 1.5})
    ex, _ = _build(tmp_path, adapter=FixtureAdapter([bad, bad]))
    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))
    assert caught.value.cause == "bad_reply"
    assert caught.value.transient is False


def test_a_money_field_that_arrives_as_a_json_number_never_validates(tmp_path):
    """The seam's money rule end to end: a model that answers 1875.0 fails
    validation instead of handing a float to the ledger (invariant 2)."""
    numeric = json.dumps({"doc_type": "invoice", "amount": 1875.0, "confidence": 0.9})
    ex, _ = _build(tmp_path, adapter=FixtureAdapter([numeric, numeric]))
    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))
    assert caught.value.cause == "bad_reply"


def test_oversize_file_is_terminal_and_never_calls_the_tier(tmp_path):
    ex, adapter = _build(tmp_path, max_file_bytes=10)
    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path, size=50))
    assert caught.value.cause == "oversize"
    assert caught.value.transient is False
    assert adapter.calls == []  # rejected before any spend


# ---------- transient failures (retried) -----------------------------------


def test_a_stalled_tier_is_transient(tmp_path):
    ex, _ = _build(tmp_path, adapter=FixtureAdapter(TimeoutError("gateway stalled")))
    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))
    assert caught.value.cause == "timeout"
    assert caught.value.transient is True


def test_a_transport_error_is_transient(tmp_path):
    ex, _ = _build(tmp_path, adapter=FixtureAdapter(ConnectionError("gateway down")))
    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))
    assert caught.value.cause == "transport_error"
    assert caught.value.transient is True


def test_transient_failure_is_retried_through_the_wrapper(tmp_path):
    # The wrapper redials a transient blip, the same RetryingExtractor that
    # guarded both provider classes before this row.
    adapter = FixtureAdapter(ConnectionError("blip"))
    inner = GatewayExtractor(_ctx(tmp_path), adapter=adapter, text_reader=lambda _p: "invoice text")
    slept: list[float] = []
    ex = RetryingExtractor(inner, attempts=3, backoff_s=(0.0,), sleeper=slept.append)

    with pytest.raises(ExtractionError):
        ex.extract(_doc(tmp_path))
    assert len(adapter.calls) == 3  # redialed the full budget


# ---------- no text layer: the tier answers, the extractor does not guess ----


def test_an_image_only_document_reaches_the_tier_as_an_attachment(tmp_path):
    """Before this row a text-only lane short-circuited to ``needs_ocr`` on a
    ``vision_model`` knob. The tier is the vision decision now: the image
    rides as an attachment and what comes back is the model's answer, OCR
    flag included, never the extractor's guess."""
    ocr = json.dumps({"doc_type": "unknown", "confidence": 0.0, "needs_ocr": True})
    ex, adapter = _build(tmp_path, text="   ", adapter=FixtureAdapter({"*": ocr}))
    doc = ex.extract(_doc(tmp_path, name="scan.png"))

    assert doc.needs_ocr is True
    assert doc.doc_type == "unknown"
    assert len(adapter.calls) == 1
    assert [(a.path.name, a.mime) for a in adapter.calls[0].attachments] == [
        ("scan.png", "image/png")
    ]
