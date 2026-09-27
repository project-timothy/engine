"""Row 7.10: AP extraction runs through the model gateway, not a provider class.

``ClaudeExtractor`` and ``QwenExtractor`` were two classes holding the same
contract with two hard-coded providers. They collapse into one
:class:`GatewayExtractor` whose provider comes from the tenant policy
(``[llm.jobs].invoice_extract`` for AP intake, ``receipt_extract`` for the
expenses pass). ``FixtureExtractor`` and ``RetryingExtractor`` are unchanged:
the sidecar extractor is still what the intake evals run on, and the retry
wrapper still reads the same ``transient`` flag.

What this eval pins:

- one gateway call per document, recorded in ``llm_calls`` (row 7.9's
  telemetry), including on the fixture adapter, so the row count is the
  proof a call happened;
- the document's text layer is inlined AND the file rides as an attachment,
  one prompt shape for every provider;
- the ``ExtractionError`` taxonomy survives the move, so the retry wrapper
  and the ledger's flag causes keep their meaning;
- ``--param extractor=`` keeps ``fixture``, ``claude``, and ``qwen`` as
  aliases onto tiers for one release, plus the new ``tier:<name>`` spelling,
  and an unknown value fails loudly naming what is legal;
- the intake run key changes when the resolved tier changes.

The W-9 deterministic pre-model detection is untouched by this row and keeps
its own evals (``test_w9_intake.py``); nothing is added in its path.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.agents.ap.extraction import (
    INVOICE_EXTRACT_JOB,
    MAX_EXTRACT_FILE_BYTES,
    ExtractionError,
    ExtractionReply,
    FixtureExtractor,
    GatewayExtractor,
    RetryingExtractor,
    build_extractor,
    resolved_tier,
)
from core.agents.ap.jobs import JOBS
from core.agents.ap.schema import ExtractedDocument
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger
from core.llm import GatewayTransportError
from core.llm.adapters.fixture import FixtureAdapter

GOOD_REPLY = json.dumps(
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


def _tenant_data(*, seat_model: str = "seat-model", with_local: bool = True) -> dict:
    """A tenant shaped like the live one: a flat-rate seat tier serving the
    extract jobs, an opt-in local tier, W-9 detection deterministic. Both
    tiers run the fixture adapter so the eval needs no network."""
    tiers: dict[str, dict] = {
        "seat": {
            "adapter": "fixture",
            "model": seat_model,
            "api_key_env": "",
            "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
        }
    }
    if with_local:
        tiers["local"] = {
            "adapter": "fixture",
            "model": "local-coder",
            "base_url": "http://localhost:4000/v1",
            "api_key_env": "ENGINE_GATEWAY_KEY",
            "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
        }
    return {
        "identity": {"legal_name": "Extraction Test Co", "slug": "demo"},
        "llm": {
            "tiers": tiers,
            "jobs": {
                "invoice_extract": "seat",
                "receipt_extract": "seat",
                "w9_detect": "deterministic",
            },
            "budget": {"monthly_usd": "25"},
        },
    }


def _ctx(tmp_path: Path, data: dict | None = None, **params) -> JobContext:
    tenant = TenantConfig.model_validate(data or _tenant_data())
    ledger = Ledger.open(tmp_path / "ledger")
    return JobContext(
        tenant=tenant,
        tenant_slug="demo",
        ledger=ledger,
        agent="ap",
        job="intake",
        params=params,
        run_key="demo.ap.intake.k1",
    )


def _doc(tmp_path: Path, name: str = "invoice.pdf", text: str = "ARROW ASSEMBLY INVOICE") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 stub")
    (tmp_path / f"{name}.txt").write_text(text, encoding="utf-8")
    return path


def _reader(text: str):
    return lambda path: text


def _rows(ctx: JobContext) -> list[dict]:
    return [
        dict(r) for r in ctx.ledger.conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()
    ]


# ---- one call, one row, one validated document --------------------------------


def test_extraction_goes_through_the_policy_and_records_one_llm_call(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    ex = GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("ARROW ASSEMBLY INVOICE"))

    doc = ex.extract(_doc(tmp_path))

    assert isinstance(doc, ExtractedDocument)
    assert doc.doc_type == "invoice"
    assert doc.vendor_name == "Arrow Assembly"
    assert doc.amount == Decimal("1875.00")  # a Decimal in code, a string on the wire
    rows = _rows(ctx)
    assert len(rows) == 1, "one gateway call, one llm_calls row (row 7.9's telemetry)"
    assert rows[0]["job_type"] == INVOICE_EXTRACT_JOB
    assert rows[0]["tier"] == "seat"
    assert rows[0]["adapter"] == "fixture"
    assert rows[0]["model"] == "seat-model"
    assert rows[0]["status"] == "ok"


def test_the_text_layer_is_inlined_and_the_file_rides_as_an_attachment(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    path = _doc(tmp_path)
    GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("TOTAL DUE $1,875.00")).extract(path)

    bundle = adapter.calls[0]
    assert bundle.job_type == INVOICE_EXTRACT_JOB
    assert bundle.model == "seat-model"
    assert "TOTAL DUE $1,875.00" in "\n".join(m.content for m in bundle.turns())
    assert [(a.path, a.mime) for a in bundle.attachments] == [(path, "application/pdf")]
    # The domain rules (a PO is "po" even when it mentions invoicing, a credit
    # memo is an invoice with a negative amount) ride the system text.
    assert "credit memo" in bundle.system_text().lower()


def test_a_file_with_no_text_layer_still_reaches_the_model_as_an_attachment(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"\x89PNG\r\n\x1a\n")
    GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("")).extract(scan)

    bundle = adapter.calls[0]
    assert [(a.path, a.mime) for a in bundle.attachments] == [(scan, "image/png")]


def test_a_text_reader_that_raises_does_not_kill_the_document(tmp_path):
    """Reading a text layer is new on the default path: a parser that throws
    must degrade to "no text", never take the whole intake run down."""
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})

    def explodes(path: Path) -> str:
        raise RuntimeError("pypdf cannot parse this")

    doc = GatewayExtractor(ctx, adapter=adapter, text_reader=explodes).extract(_doc(tmp_path))
    assert doc.doc_type == "invoice"


# ---- the error taxonomy survives the move ------------------------------------


def test_an_unparseable_reply_is_a_terminal_bad_reply(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(["not json at all", "still not json"])
    ex = GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("text"))

    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))

    assert caught.value.cause == "bad_reply"
    assert caught.value.transient is False  # a redial cannot fix content
    assert len(_rows(ctx)) == 1
    assert _rows(ctx)[0]["status"] == "validation_failed"


def test_a_transport_failure_stays_transient_for_the_retry_wrapper(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(
        GatewayTransportError("the pipe broke", cause="transport_error", transient=True)
    )
    ex = GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("text"))

    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))

    assert caught.value.cause == "transport_error"
    assert caught.value.transient is True


def test_a_timeout_keeps_its_cause_and_stays_transient(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(GatewayTransportError("too slow", cause="timeout", transient=True))
    ex = GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("text"))

    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))

    assert caught.value.cause == "timeout"
    assert caught.value.transient is True


def test_a_budget_refusal_routes_the_document_to_review_instead_of_crashing(tmp_path):
    data = _tenant_data()
    data["llm"]["budget"]["monthly_usd"] = "0"
    ctx = _ctx(tmp_path, data)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    ex = GatewayExtractor(ctx, adapter=adapter, text_reader=_reader("text"))

    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))

    assert caught.value.cause == "budget_exceeded"
    assert caught.value.transient is False
    assert adapter.calls == [], "the refusal lands before any provider is touched"


def test_a_policy_gap_names_the_job_and_never_retries(tmp_path):
    data = _tenant_data()
    data["llm"]["jobs"] = {"w9_detect": "deterministic"}
    ctx = _ctx(tmp_path, data)
    ex = GatewayExtractor(ctx, adapter=FixtureAdapter({"*": GOOD_REPLY}))

    with pytest.raises(ExtractionError) as caught:
        ex.extract(_doc(tmp_path))

    assert caught.value.cause == "policy"
    assert caught.value.transient is False
    assert INVOICE_EXTRACT_JOB in str(caught.value)


def test_the_oversize_cap_still_fails_before_any_call(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    big = tmp_path / "oversized_drawing.pdf"
    with big.open("wb") as fh:
        fh.seek(MAX_EXTRACT_FILE_BYTES)
        fh.write(b"\0")

    with pytest.raises(ExtractionError, match="extraction cap") as caught:
        GatewayExtractor(ctx, adapter=adapter).extract(big)

    assert caught.value.cause == "oversize"
    assert caught.value.transient is False
    assert adapter.calls == []
    assert _rows(ctx) == [], "a file refused by the cap costs no model call"


# ---- the aliases -------------------------------------------------------------


def test_the_claude_alias_maps_to_the_tier_the_policy_names_for_the_job(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "claude", INVOICE_EXTRACT_JOB) == ("seat", "fixture", "seat-model")
    live = build_extractor("claude", ctx)
    assert isinstance(live, RetryingExtractor)


def test_the_qwen_alias_maps_to_the_local_tier_when_the_tenant_defines_one(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "qwen", INVOICE_EXTRACT_JOB) == ("local", "fixture", "local-coder")


def test_the_qwen_alias_falls_back_to_the_policy_when_there_is_no_local_tier(tmp_path):
    ctx = _ctx(tmp_path, _tenant_data(with_local=False))
    assert resolved_tier(ctx, "qwen", INVOICE_EXTRACT_JOB) == ("seat", "fixture", "seat-model")


def test_a_tier_can_be_named_directly(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "tier:local", INVOICE_EXTRACT_JOB) == (
        "local",
        "fixture",
        "local-coder",
    )
    assert isinstance(build_extractor("tier:local", ctx), RetryingExtractor)


def test_the_fixture_alias_needs_no_tenant_policy_at_all(tmp_path):
    assert isinstance(build_extractor("fixture"), FixtureExtractor)
    assert resolved_tier(_ctx(tmp_path), "fixture", INVOICE_EXTRACT_JOB) is None


def test_an_unknown_extractor_fails_loudly_with_the_list(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError) as caught:
        build_extractor("gpt5", ctx)
    message = str(caught.value)
    for legal in ("fixture", "claude", "qwen", "tier:"):
        assert legal in message


def test_a_tier_that_does_not_exist_fails_loudly_naming_the_tenant_tiers(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError) as caught:
        build_extractor("tier:ghost", ctx)
    message = str(caught.value)
    assert "ghost" in message
    assert "seat" in message and "local" in message


def test_a_live_extractor_without_a_context_says_so(tmp_path):
    with pytest.raises(ValueError, match="tenant"):
        build_extractor("claude")


# ---- the run key -------------------------------------------------------------


def _intake_key(tmp_path: Path, data: dict) -> str:
    landing = tmp_path / "landing"
    landing.mkdir(exist_ok=True)
    (landing / "one.pdf").write_bytes(b"%PDF-1.4 stub")
    ctx = _ctx(
        tmp_path,
        data,
        landing_dir=str(landing),
        vendors_toml=str(tmp_path / "no-vendors.toml"),
        extractor="claude",
    )
    return JOBS["intake"].key(ctx)


def test_the_intake_run_key_changes_when_the_resolved_tier_changes(tmp_path):
    same = _intake_key(tmp_path, _tenant_data())
    assert same == _intake_key(tmp_path, _tenant_data()), "same policy, same key, no work"
    moved = _intake_key(tmp_path, _tenant_data(seat_model="a-different-model"))
    assert moved != same, "the run key must follow the resolved tier, adapter, and model"


def test_the_intake_run_key_changes_when_the_job_is_pointed_at_another_tier(tmp_path):
    same = _intake_key(tmp_path, _tenant_data())
    data = _tenant_data()
    data["llm"]["jobs"]["invoice_extract"] = "local"
    assert _intake_key(tmp_path, data) != same


# ---- the reply contract ------------------------------------------------------


def test_the_reply_model_mirrors_the_document_and_carries_money_as_text():
    assert set(ExtractionReply.model_fields) == set(ExtractedDocument.model_fields)
    reply = ExtractionReply.model_validate({"doc_type": "invoice", "amount": "-120.00"})
    assert reply.amount == "-120.00"  # a STRING on the wire (the gateway money rule)
    doc = reply.to_document()
    assert doc.amount == Decimal("-120.00")  # code re-parses it (invariant 2)
    assert ExtractionReply().to_document().amount is None


def test_a_json_number_for_money_never_validates():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExtractionReply.model_validate({"doc_type": "invoice", "amount": 1875.0})
