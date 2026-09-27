"""Incident regression: 2026-06-11 SDK transport buffer overflow.

Bad pattern: an extraction input larger than the transport can carry is sent
anyway and dies inside the SDK with a buffer error, instead of the extractor
enforcing an explicit size cap and failing fast with a diagnosable message.
See docs/lessons.md, "Oversize inputs fail fast".

Retargeted in phase 7 row 7.10, intent preserved. The cap stays with the
extractor (refusing an oversize file costs no model call, whatever tier is
configured); the transport buffer that must clear the cap moved to the SDK
adapter with the SDK call itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.ap.extraction import (
    MAX_EXTRACT_FILE_BYTES,
    ExtractionError,
    GatewayExtractor,
)
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger
from core.llm.adapters.claude_sdk_complete import SDK_BUFFER_BYTES, ClaudeSdkCompleteAdapter
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.gateway import Message, PromptBundle

TENANT = {
    "identity": {"legal_name": "Size Cap Test Co", "slug": "demo"},
    "llm": {
        "tiers": {
            "seat": {
                "adapter": "fixture",
                "model": "seat-model",
                "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
            }
        },
        "jobs": {"invoice_extract": "seat"},
    },
}

GOOD = '{"doc_type": "invoice", "confidence": 0.9}'


def _ctx(tmp_path: Path) -> JobContext:
    return JobContext(
        tenant=TenantConfig.model_validate(TENANT),
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.k1",
    )


def _file_of_size(tmp_path, name: str, size: int):
    path = tmp_path / name
    with path.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\0")
    assert path.stat().st_size == size
    return path


def _extractor(tmp_path, adapter):
    return GatewayExtractor(_ctx(tmp_path), adapter=adapter, text_reader=lambda _p: "text")


def test_oversized_file_fails_fast_without_a_model_call(tmp_path):
    big = _file_of_size(tmp_path, "oversized_drawing.pdf", MAX_EXTRACT_FILE_BYTES + 1)
    adapter = FixtureAdapter({"*": GOOD})

    with pytest.raises(ExtractionError, match="extraction cap"):
        _extractor(tmp_path, adapter).extract(big)

    assert adapter.calls == []


def test_oversized_failure_names_size_and_cap(tmp_path):
    big = _file_of_size(tmp_path, "oversized_drawing.pdf", MAX_EXTRACT_FILE_BYTES + 1)
    with pytest.raises(ExtractionError) as excinfo:
        _extractor(tmp_path, FixtureAdapter({"*": GOOD})).extract(big)
    message = str(excinfo.value)
    assert str(MAX_EXTRACT_FILE_BYTES + 1) in message
    assert str(MAX_EXTRACT_FILE_BYTES) in message


def test_file_under_the_cap_is_extracted(tmp_path):
    doc = _file_of_size(tmp_path, "large_scan.pdf", 5 * 1024 * 1024)
    result = _extractor(tmp_path, FixtureAdapter({"*": GOOD})).extract(doc)
    assert result.doc_type == "invoice"


def test_sdk_buffer_clears_the_cap_with_base64_headroom():
    assert SDK_BUFFER_BYTES >= MAX_EXTRACT_FILE_BYTES * 4 // 3


def test_the_sdk_adapter_puts_that_buffer_on_the_real_options():
    # Builds the real ClaudeAgentOptions: needs the [claude] extra (row 7.15).
    pytest.importorskip(
        "claude_agent_sdk",
        reason="the Claude Agent SDK is not installed: the [claude] extra (uv sync --extra claude)",
    )
    import claude_agent_sdk

    bundle = PromptBundle("invoice_extract", "default", (Message("user", "classify"),), (), 120)
    options = ClaudeSdkCompleteAdapter()._options(claude_agent_sdk, bundle)
    assert options.max_buffer_size is not None
    assert options.max_buffer_size >= MAX_EXTRACT_FILE_BYTES * 4 // 3
