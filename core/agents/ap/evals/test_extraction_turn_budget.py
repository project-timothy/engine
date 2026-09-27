"""Incident regression: 2026-09-15 a scanned fax receipt could not be
extracted inside four SDK turns.

Today's 08:00 run flagged `brw849e567b27c7_000208.pdf` with
`cause: transport_error`, "extraction failed", after the retry wrapper spent
all three attempts. Reproducing the pre-row-7.10 extractor call by hand (its
prompt, the Read tool, ``max_turns=4``, the 32 MB buffer) raised
``Reached maximum number of turns (4)``, so the bound was the wall, not the
pipe: reading a multi-page scan costs several tool turns before the model has
anything to answer from, and no number of redials buys another turn.

Two halves of the fix, both measured on the live seat against that document:
the bound is higher than 4, and the extractor inlines the text layer alongside
the attached file, which is what lets the model stop reading and answer (with
the file alone the document still exhausted 8 turns).

See docs/lessons.md, "Budget for the hardest input".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.ap.extraction import ExtractionError, GatewayExtractor
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger
from core.llm.adapters.claude_sdk_complete import MAX_TURNS, ClaudeSdkCompleteAdapter
from core.llm.gateway import Attachment, Message, PromptBundle

# What the failing document needed: more than four turns, and more than the
# file alone. Both numbers come from live runs, not from taste.
TURNS_THE_DOCUMENT_NEEDED = 8

TENANT = {
    "identity": {"legal_name": "Turn Budget Test Co", "slug": "demo"},
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


def _ctx(tmp_path: Path) -> JobContext:
    return JobContext(
        tenant=TenantConfig.model_validate(TENANT),
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.k1",
    )


def test_the_turn_bound_clears_what_the_failing_document_needed():
    assert MAX_TURNS >= TURNS_THE_DOCUMENT_NEEDED, (
        f"a scanned fax receipt needed {TURNS_THE_DOCUMENT_NEEDED} turns on the live seat; "
        f"a bound of {MAX_TURNS} puts that class of document permanently in review"
    )


def test_the_bound_reaches_the_sdk_options(monkeypatch, tmp_path):
    """Declaring the constant is not enough: it must be on the options object
    the SDK is handed, which is where the old 4 actually lived."""
    from core.llm.adapters import claude_sdk_complete as mod

    seen: dict = {}

    class _Options:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    class _Sdk:
        ClaudeAgentOptions = _Options

    monkeypatch.setattr(mod, "sdk", lambda: _Sdk())
    monkeypatch.setattr(
        mod,
        "stream",
        lambda prompt, options: _one('{"doc_type": "receipt", "confidence": 0.9}'),
    )
    doc = tmp_path / "scan.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    bundle = PromptBundle(
        "invoice_extract",
        "default",
        (Message("user", "classify"),),
        (Attachment(doc, "application/pdf"),),
        120,
    )
    ClaudeSdkCompleteAdapter(scratch_root=tmp_path / "scratch").complete(bundle, {})
    assert seen["max_turns"] == MAX_TURNS
    # Retargeted for issue #265 (intent preserved): the list is a pre-approval
    # list, not a restriction, so what it names has moved twice. It gained Bash
    # when the session rendered its own pages and lost it again when the engine
    # took the rendering over (#296). A scan is unreadable without the Read
    # tool either way, which is what this line has always been about.
    assert "Read" in seen["allowed_tools"], "a scan is unreadable without the Read tool"


def _one(text: str):
    class _Result:
        def __init__(self) -> None:
            self.result = text
            self.usage = {}
            self.model = None

    async def gen():
        yield _Result()

    return gen()


def test_the_text_layer_rides_with_the_attachment_not_instead_of_it(tmp_path, monkeypatch):
    """The prompt half of the fix. With the file alone the document exhausted
    its turns; with the text layer in the prompt it answered. Both must be on
    the wire, so neither half can be dropped as an optimisation without this
    failing."""
    from core.llm.adapters.fixture import FixtureAdapter

    adapter = FixtureAdapter({"*": '{"doc_type": "receipt", "confidence": 0.9}'})
    doc = tmp_path / "brw_scan.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    GatewayExtractor(
        _ctx(tmp_path), adapter=adapter, text_reader=lambda _p: "OFFICE DEPOT  TOTAL 6.00"
    ).extract(doc)

    bundle = adapter.calls[0]
    assert "OFFICE DEPOT" in "\n".join(m.content for m in bundle.turns())
    assert [a.path for a in bundle.attachments] == [doc]


def test_a_turn_cap_error_is_transient_so_the_retry_wrapper_may_redial(tmp_path):
    """Even with a better bound, a document that trips it must come back as a
    transient failure carrying the SDK's own words, so the flag is
    diagnosable instead of reading as a flaky pipe with no explanation."""
    from core.llm.adapters.fixture import FixtureAdapter

    capped = RuntimeError("Claude Code returned an error result: Reached maximum number of turns")
    doc = tmp_path / "scan.pdf"
    doc.write_bytes(b"%PDF-1.4 stub")
    ex = GatewayExtractor(
        _ctx(tmp_path), adapter=FixtureAdapter(capped), text_reader=lambda _p: "text"
    )
    with pytest.raises(ExtractionError) as caught:
        ex.extract(doc)
    assert caught.value.cause == "transport_error"
    assert caught.value.transient is True
    assert "maximum number of turns" in str(caught.value)
