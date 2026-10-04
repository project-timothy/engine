"""Receipt-inbox classification boundary (issue #112): label-only, by contract.

The Taildrop watcher sweeps EVERY iPhone camera image into the inbox, so
personal-photo strays sit alongside receipts (the 2026-08-11 sweep incident
is the cautionary case). The owner's guardrail decision (2026-08-12):

    LABEL-ONLY — the classifier emits {receipt, confidence} and, for
    receipts only, {vendor, amount, expense_date}. It never transcribes,
    describes, or characterizes non-receipt content, and non-receipt
    images never attach content to cards.

``sanitize`` enforces the contract in CODE regardless of what a model
returns: a non-receipt label is stripped to the bare boolean + confidence.
Evals pin this as an invariant.

Two implementations of one interface, the ap.extraction shape:

- :class:`GatewayInboxClassifier` (live): one model call through the seam
  (``core.llm.policy.complete_for``), so the provider, the model id, the
  price, the monthly budget, and the ``llm_calls`` row all come from the
  tenant's ``[llm]`` tables rather than from a provider class. Phase 7 row
  7.11 replaced ``ClaudeInboxClassifier`` (the Agent SDK hard-coded, with
  its own turn bound and transport buffer) with this one; which model
  answers is now ``[llm.tiers]`` plus ``[llm.jobs].inbox_classify``, and the
  transport settings belong to the adapter the tier names.
- :class:`FixtureInboxClassifier` (tests/CI): reads a ``<file>.label.json``
  sidecar. Deterministic, no network, no secrets.

The guardrail did not move with the transport: every reply, whatever tier
produced it, goes through :func:`sanitize` before it leaves this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.llm import Attachment, Message

from ..ap.extraction import document_mime

LABEL_SIDECAR_SUFFIX = ".label.json"

INBOX_CLASSIFY_JOB = "inbox_classify"
"""The policy key for the receipt-inbox pre-stage (``[llm.jobs].inbox_classify``)."""

SITE = "inbox classifier"
"""What an unknown ``--param classifier=`` value is called in the error."""

# The guardrail, as the model sees it. The reply SHAPE is enforced twice over
# on top of this (the gateway appends the JSON schema as a system turn and
# pydantic validates what comes back), and the CONTENT rule is enforced a
# third time, in code, by sanitize().
_LABEL_SPEC = (
    "You are a privacy-constrained triage step for a receipt inbox. Decide "
    "ONLY whether the attached image is a purchase receipt (a printed or "
    "handwritten record of a purchase: store receipt, restaurant check, "
    "invoice-style ticket).\n"
    "Fill vendor (merchant name), amount (dollars, e.g. 12.34), and "
    "expense_date (YYYY-MM-DD) ONLY when receipt is true and the value is "
    "legible. If the image is NOT a receipt you must set receipt to false "
    "and leave every other string empty: do not describe, transcribe, "
    "summarize, or characterize its content in any way."
)

_LABEL_ASK = "Classify the attached image for the receipt inbox."


class InboxLabel(BaseModel):
    """What classification may assert about one inbox image. Nothing else."""

    receipt: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    vendor: str = ""
    amount: str = ""  # dollars as text; a proposal, never arithmetic input
    expense_date: str = ""


def sanitize(label: InboxLabel) -> InboxLabel:
    """Enforce the label-only contract in code: a non-receipt carries the
    boolean and the confidence, nothing more — whatever a model said."""
    if label.receipt:
        return label
    return InboxLabel(receipt=False, confidence=label.confidence)


def _unreadable() -> InboxLabel:
    """The safe answer when no usable label came back: not a receipt, no
    confidence, no content. The image stays where it is and parks a skip
    card, which is a decision a human makes."""
    return sanitize(InboxLabel(receipt=False, confidence=0.0))


class FixtureInboxClassifier:
    """Deterministic classifier for evals: reads a JSON sidecar per file."""

    def classify(self, path: Path) -> InboxLabel:
        sidecar = path.with_name(path.name + LABEL_SIDECAR_SUFFIX)
        if not sidecar.exists():
            return _unreadable()
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        return sanitize(InboxLabel.model_validate(raw))


class GatewayInboxClassifier:
    """Live classification through the model seam.

    One call per image: ``complete_for(ctx, job_type, ...)`` resolves the tier
    from the tenant policy, prices it, refuses it past the monthly budget,
    records it in ``llm_calls``, and validates the reply into
    :class:`InboxLabel`. Everything provider-specific (the wire format, the
    turn bound, the transport buffer) belongs to the adapter the tier names.

    Failure policy, unchanged from the pre-gateway classifier: a reply the
    engine cannot use answers "not a receipt" with no confidence (the image
    waits for a human on a skip card), and everything else — a transport
    failure, a missing ``[claude]`` extra, a policy gap, a spent budget —
    propagates, so the run fails loudly with its trace (#172) instead of
    writing a wrong label into event memory that is never looked at twice.

    ``tier`` pins a tier by name instead of resolving the job type, which is
    what ``--param classifier=tier:<name>`` selects. ``adapter`` overrides the
    wire for every attempt (tests and evals hand in a seeded fixture adapter);
    ``completer`` is injected so a test can run with no policy at all.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        job_type: str = INBOX_CLASSIFY_JOB,
        tier: str | None = None,
        timeout_s: int = 120,
        adapter: Any | None = None,
        completer: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._job_type = job_type
        self._tier = tier
        self._timeout_s = timeout_s
        self._adapter = adapter
        self._complete = completer

    def classify(self, path: Path) -> InboxLabel:
        from core.llm import GatewayValidationError
        from core.llm.policy import complete_for

        messages = [Message("system", _LABEL_SPEC), Message("user", _LABEL_ASK)]
        call = self._complete or complete_for
        try:
            result = call(
                self._ctx,
                self._job_type,
                messages,
                InboxLabel,
                attachments=[Attachment(path, document_mime(path))],
                timeout_s=self._timeout_s,
                adapter=self._adapter,
                tier=self._tier,
                # A validation error QUOTES the reply, so the failure text of
                # this job never reaches the ledger (owner decision
                # 2026-08-12; the status column still names the failure).
                redact_detail=True,
            )
        except GatewayValidationError:
            # The model answered something unusable. Never the reply, never a
            # paraphrase: the safe label, exactly as before the gateway.
            return _unreadable()
        return sanitize(result.output)


def resolved_tier(ctx: Any, kind: str, job_type: str = INBOX_CLASSIFY_JOB) -> tuple | None:
    """``(tier, adapter, model)`` for what this classifier name would call, or
    ``None`` for the fixture classifier (which calls no model at all). The
    inbox run key folds it in, so the key moves when the tier does."""
    from core.llm.policy import resolved_tier_for_alias

    return resolved_tier_for_alias(ctx.tenant.llm, kind, job_type, site=SITE)


def build_classifier(
    kind: str, ctx: Any | None = None, *, job_type: str = INBOX_CLASSIFY_JOB
) -> Any:
    """Resolve a classifier by name.

    ``fixture`` is the sidecar classifier (tests, and the only name that needs
    no tenant policy). ``claude`` is the tier the policy names for this job and
    stays an alias for one release; ``tier:<name>`` says it directly. Anything
    else raises naming what is legal.
    """
    from core.llm.policy import FIXTURE_ALIAS, tier_for_alias

    if kind == FIXTURE_ALIAS:
        return FixtureInboxClassifier()
    if ctx is None:
        raise ValueError(
            f"{SITE} {kind!r} calls a model, so it needs the job's tenant context; "
            f"only {FIXTURE_ALIAS!r} runs without one"
        )
    tier = tier_for_alias(ctx.tenant.llm, kind, site=SITE)
    return GatewayInboxClassifier(ctx, job_type=job_type, tier=tier)
