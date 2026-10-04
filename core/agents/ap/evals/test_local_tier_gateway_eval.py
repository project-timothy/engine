"""Gated eval: does the configured open-weight model extract AP fields well
enough to trust on the real intake flow?

This is a *gated* eval, not a unit test. It needs a live LiteLLM gateway, so it
cannot run in CI. The guard at the top skips the whole module unless
``ENGINE_GATEWAY_KEY`` is set and the gateway answers. Run it on demand after
swapping a model:

    ENGINE_GATEWAY_KEY=<master-key> \
    ENGINE_GATEWAY_EVAL_MODEL=local-coder \
    uv run pytest core/agents/ap/evals/test_local_tier_gateway_eval.py -s

Retargeted in phase 7 row 7.10, intent preserved: the lane under test is a
TIER on the OpenAI-compatible adapter now, not a provider class, so the eval
builds a one-tier tenant pointed at the gateway and runs the production
extractor against it.

The documents are synthetic with fake vendors, so nothing proprietary leaves
the machine even when the eval points at a hosted model. The hard assertion is
the five standard AP document types (invoice, po, quote, statement, receipt),
the workload the intake flow actually sees; the credit-memo case is reported
too, since it validates the "a credit memo is an invoice with a negative
amount" prompt rule end to end.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from decimal import Decimal

import pytest

from core.agents.ap.extraction import ExtractionError, GatewayExtractor, RetryingExtractor
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger

GATEWAY = os.environ.get("ENGINE_GATEWAY_URL", "http://localhost:4000/v1")
MODEL = os.environ.get("ENGINE_GATEWAY_EVAL_MODEL", "local-coder")


def _gateway_reachable() -> bool:
    key = os.environ.get("ENGINE_GATEWAY_KEY")
    if not key:
        return False
    req = urllib.request.Request(f"{GATEWAY}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - localhost gateway
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


pytestmark = pytest.mark.skipif(
    not _gateway_reachable(),
    reason="gateway not reachable (set ENGINE_GATEWAY_KEY and start the gateway)",
)

# (name, text, expected_doc_type, expected_vendor_substr, expected_amount)
_STANDARD = [
    (
        "inv_alpha.txt",
        "ALPHA PARTS LLC\nINVOICE\nInvoice #: 1001\nDate: 2026-06-01\n"
        "Due: 2026-07-01\nWidgets x100\nTotal Due: $450.00\nRemit to Alpha Parts LLC",
        "invoice",
        "alpha",
        Decimal("450.00"),
    ),
    (
        "po_beta.txt",
        "BETA FREIGHT CO\nPURCHASE ORDER\nPO Number: 77\nShip freight services as "
        "described. Please invoice against this PO on completion.\nNot-to-exceed: $2,000.00",
        "po",
        "beta",
        None,
    ),
    (
        "quote_gamma.txt",
        "GAMMA TOOLING\nQUOTATION  Q-500\nThis is a price quote, not an invoice. "
        "Valid 30 days.\nEstimated total: $8,900.00\nNo payment due at this time.",
        "quote",
        "gamma",
        None,
    ),
    (
        "stmt_delta.txt",
        "DELTA SUPPLY\nSTATEMENT OF ACCOUNT\nPeriod: June 2026\nInv 900 $400.00\n"
        "Inv 901 $830.00\nAccount balance due: $1,230.00",
        "statement",
        "delta",
        None,
    ),
    (
        "receipt_od.txt",
        "OFFICE DEPOT\nSALES RECEIPT\nPrinter paper, pens\nPaid by VISA ****1234\n"
        "Total: $34.99\nThank you for your purchase",
        "receipt",
        None,
        Decimal("34.99"),
    ),
]

_CREDIT_MEMO = (
    "credit_alpha.txt",
    "ALPHA PARTS LLC\nCREDIT MEMO  CM-12\nReturn of defective widgets\n"
    "Credit amount: -$120.00\nApplied to invoice 1001",
)


def _ctx(tmp_path) -> JobContext:
    tenant = TenantConfig.model_validate(
        {
            "identity": {"legal_name": "Local Tier Eval Co", "slug": "demo"},
            "llm": {
                "tiers": {
                    "local": {
                        "adapter": "openai_compat",
                        "model": MODEL,
                        "base_url": GATEWAY,
                        "api_key_env": "ENGINE_GATEWAY_KEY",
                        "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
                    }
                },
                "jobs": {"invoice_extract": "local"},
            },
        }
    )
    return JobContext(
        tenant=tenant,
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.eval",
    )


def _extractor(tmp_path) -> RetryingExtractor:
    # Wrapped in the same retry the production path uses, so a one-off transport
    # blip on a free/hosted model does not fail the eval spuriously.
    return RetryingExtractor(GatewayExtractor(_ctx(tmp_path), tier="local", timeout_s=60))


def test_standard_doc_types_classify_correctly(tmp_path):
    ex = _extractor(tmp_path)
    failures = []
    for name, text, want_type, want_vendor, want_amount in _STANDARD:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        try:
            doc = ex.extract(path)
        except ExtractionError as e:
            failures.append(f"{name}: extraction error cause={e.cause}")
            continue
        print(f"{name:<16} type={doc.doc_type:<9} vendor={doc.vendor_name!r} amount={doc.amount}")
        if doc.doc_type != want_type:
            failures.append(f"{name}: doc_type {doc.doc_type!r} != {want_type!r}")
        if want_vendor and want_vendor not in (doc.vendor_name or "").lower():
            failures.append(f"{name}: vendor {doc.vendor_name!r} missing {want_vendor!r}")
        if want_amount is not None and doc.amount != want_amount:
            failures.append(f"{name}: amount {doc.amount} != {want_amount}")
    assert not failures, f"model={MODEL} missed:\n" + "\n".join(failures)


def test_credit_memo_is_invoice_with_negative_amount(tmp_path):
    # Validates the shared prompt rule end to end. A model that emits a
    # "credit memo" doc_type would fail validation (bad_reply); the rule steers
    # it to invoice + negative amount instead.
    name, text = _CREDIT_MEMO
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    doc = _extractor(tmp_path).extract(path)
    print(f"{name:<16} type={doc.doc_type:<9} amount={doc.amount}")
    assert doc.doc_type == "invoice"
    assert doc.amount is not None and doc.amount < 0
