"""Incident regression: 2026-06-19 intake hang on a stalled extraction call.

Bad pattern: the extractor declares ``timeout_s`` but never enforces it, so a
single file that stalls the model transport blocks the entire intake run
indefinitely instead of failing that one file fast and routing it to review.
See docs/lessons.md, "A declared bound is an enforced bound".

Retargeted in phase 7 row 7.10, intent preserved. The bound now sits in two
places and this eval pins both halves:

- the wall clock actually fires (the SDK adapter's ``asyncio.wait_for``, so a
  wedged transport is torn down rather than waited out);
- the extractor still turns that into a transient ``timeout``
  ``ExtractionError`` naming the file and the bound, which is what routes the
  one document to review and lets the retry wrapper redial.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from core.agents.ap.extraction import ExtractionError, GatewayExtractor
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger
from core.llm.adapters.fixture import FixtureAdapter

SEAT_TENANT = {
    "identity": {"legal_name": "Timeout Test Co", "slug": "demo"},
    "llm": {
        "tiers": {
            "seat": {
                "adapter": "claude_agent_sdk",
                "model": "default",
                "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
            }
        },
        "jobs": {"invoice_extract": "seat"},
    },
}


def _ctx(tmp_path: Path, data: dict | None = None) -> JobContext:
    return JobContext(
        tenant=TenantConfig.model_validate(data or SEAT_TENANT),
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.k1",
    )


def _stub_sdk(monkeypatch, *, sleep_s: float, reply: str = "{}"):
    """Stand in for the Claude Agent SDK with a transport that sleeps."""
    from core.llm.adapters import claude_sdk_complete as mod

    class _Options:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Result:
        def __init__(self, text):
            self.result = text
            self.usage = {}
            self.model = None

    class _Sdk:
        ClaudeAgentOptions = _Options
        HookMatcher = staticmethod(lambda matcher=None, hooks=None: (matcher, hooks))

    def stream(prompt, options):
        async def gen():
            await asyncio.sleep(sleep_s)
            yield _Result(reply)

        return gen()

    monkeypatch.setattr(mod, "sdk", lambda: _Sdk())
    monkeypatch.setattr(mod, "stream", stream)


def test_stalled_extraction_times_out_instead_of_hanging(tmp_path, monkeypatch):
    path = tmp_path / "wedges_the_sdk.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    _stub_sdk(monkeypatch, sleep_s=30)  # a wedged transport that never replies
    extractor = GatewayExtractor(_ctx(tmp_path), timeout_s=1, text_reader=lambda _p: "text")

    start = time.monotonic()
    with pytest.raises(ExtractionError, match="timed out"):
        extractor.extract(path)
    elapsed = time.monotonic() - start
    # The bound must actually fire, well under the 30s simulated stall. Without
    # the timeout wired in, this waits the full sleep and the assert fails.
    assert elapsed < 10, f"timeout not enforced; extract() took {elapsed:.1f}s"


def test_timeout_message_names_the_file_and_the_bound(tmp_path):
    path = tmp_path / "stalls.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    extractor = GatewayExtractor(
        _ctx(tmp_path),
        timeout_s=1,
        adapter=FixtureAdapter(TimeoutError("the transport never answered")),
        text_reader=lambda _p: "text",
    )
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(path)
    message = str(excinfo.value)
    assert "stalls.pdf" in message
    assert "1" in message  # the timeout seconds appear in the message
    assert excinfo.value.cause == "timeout"
    assert excinfo.value.transient is True  # the retry wrapper may redial


def test_fast_extraction_under_the_bound_returns_normally(tmp_path, monkeypatch):
    path = tmp_path / "ok.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    _stub_sdk(monkeypatch, sleep_s=0, reply='{"doc_type": "invoice", "confidence": 0.9}')
    extractor = GatewayExtractor(_ctx(tmp_path), timeout_s=5, text_reader=lambda _p: "text")
    assert extractor.extract(path).doc_type == "invoice"
