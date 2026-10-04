"""Document extraction: the only LLM touchpoint in the AP flow.

Two implementations of one interface:

- :class:`GatewayExtractor` (live): one model call through the seam
  (``core.llm.policy.complete_for``), so the provider, the model id, the
  price, the monthly budget, and the ``llm_calls`` row all come from the
  tenant's ``[llm]`` tables rather than from a provider class. Phase 7 row
  7.10 collapsed the two provider classes (``ClaudeExtractor`` on the Agent
  SDK, ``QwenExtractor`` on the local OpenAI-compatible gateway) into this
  one; which of them answers is now ``[llm.tiers]`` plus
  ``[llm.jobs].invoice_extract``.
- :class:`FixtureExtractor` (tests/CI): reads a ``<file>.extract.json``
  sidecar. Deterministic, no network, no secrets, which is what keeps CI
  fixture-based.

Both return :class:`ExtractedDocument`; the pydantic validation is the
boundary (invariant 2). A reply that does not validate is an extraction
failure, never something downstream code "works around". The gateway
validates into :class:`ExtractionReply`, whose money field is a decimal
STRING, and CODE re-parses it into the ``Decimal`` on
:class:`ExtractedDocument` (the seam's money rule: a JSON number never
becomes a money value).

:class:`RetryingExtractor` is unchanged and still the wrapper every live
extractor rides: it reads :class:`ExtractionError`'s ``transient`` flag, and
the gateway's transport taxonomy maps onto that flag one for one.

The W-9 deterministic pre-model detection (``core/agents/ap/w9.py``) is
untouched by this module: a detected form is routed away BEFORE any extractor
is asked, so no TIN ever reaches a model, whatever tier serves the job.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from core.llm import Attachment, DecimalString, Message

from .schema import DocType, ExtractedDocument

SIDECAR_SUFFIX = ".extract.json"

INVOICE_EXTRACT_JOB = "invoice_extract"
"""The policy key for AP intake (``[llm.jobs].invoice_extract``). The expenses
receipt pass uses ``receipt_extract`` and shares this module."""

# The Read tool result streams back through the CLI transport as one JSON
# message, base64-inflated by ~4/3, so the SDK buffer must clear the file cap
# with headroom. The SDK's 1 MiB default broke on multi-MB landing PDFs
# (docs/lessons.md, "Oversize inputs fail fast"). The buffer itself now lives with the SDK
# adapter (core/llm/adapters/claude_sdk_complete.py); the cap stays here,
# because refusing an oversize file is the extractor's decision and costs no
# model call whatever provider is configured.
MAX_EXTRACT_FILE_BYTES = 20 * 1024 * 1024

# The domain brief, shared by every tier: whichever model answers, it answers
# about the same field meanings and the same classification rules. The reply
# SHAPE is enforced twice over on top of this: the gateway appends the JSON
# schema as a system turn, and pydantic validates what comes back.
_JSON_SPEC = """\
Reply with ONLY a JSON object (no prose, no code fences) with keys:
  doc_type: one of invoice|po|quote|statement|receipt|proposal|reference|unknown
  vendor_name: the issuing vendor's name as printed, or null
  invoice_number: the invoice number, or null
  amount: the total due as a decimal string (negative for credit memos), or null
  invoice_date: ISO YYYY-MM-DD, or null
  due_date: ISO YYYY-MM-DD, or null
  confidence: 0.0-1.0 for the doc_type + field extraction overall
  warnings: array of strings for anything ambiguous
  needs_ocr: true only if the file is image-only with no extractable text
  category: for a purchase receipt only, one expense category word
    (Meals|Travel|Lodging|Fuel|Supplies|Other); null for anything else
Rules: a purchase order is "po" even if it mentions invoicing; a vendor
proposal/quote is never an invoice; a photo of a machine or a screenshot is
"reference". A credit memo is doc_type "invoice" with a negative amount, not a
type of its own. Do not guess an amount that is not printed in the document.
"""

# The ask, one shape for every provider: the file is attached (an adapter that
# can read a file reads it; the SDK adapter turns it into a Read) and the text
# layer is inlined when the document has one, which is the only thing a
# text-only model can work from.
_TEXT_ASK = (
    "Classify the attached document for an accounts-payable intake system. "
    "Its text layer is below; prefer what you can see in the file itself when "
    "the two disagree.\n\n"
    "-----BEGIN DOCUMENT TEXT-----\n{text}\n-----END DOCUMENT TEXT-----"
)

_ATTACHED_ASK = (
    "Classify the attached document for an accounts-payable intake system. "
    "No text layer could be pulled from it, so read the file itself; set "
    "needs_ocr only if you cannot read it at all."
)

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heic": "image/heic",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

_TEXT_SUFFIXES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv"}


def document_mime(path: Path) -> str:
    """The MIME type an adapter needs for this file. PDFs and images are the
    shapes every adapter accepts; anything else rides as octet-stream and the
    inlined text layer is what the model actually reads."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    return _IMAGE_MIME.get(suffix) or _TEXT_SUFFIXES.get(suffix) or "application/octet-stream"


class ExtractionError(RuntimeError):
    """A file could not be extracted.

    ``cause`` labels the failure (``timeout`` / ``transport_error`` /
    ``oversize`` / ``bad_reply`` / ``sdk_missing`` / ``budget_exceeded`` /
    ``policy``) so a drop is diagnosable from the ledger. ``transient`` says
    whether retrying could plausibly succeed: a timeout or a transport blip
    might clear on a redial, but an oversize file, an unparseable reply, an
    uninstalled SDK (the ``[claude]`` extra, row 7.15), a spent monthly
    budget, or a tenant policy gap will not, so retrying it only wastes time
    and money (docs/lessons.md, "Transient is not terminal").
    """

    def __init__(self, message: str, *, cause: str = "unknown", transient: bool = False) -> None:
        super().__init__(message)
        self.cause = cause
        self.transient = transient


class Extractor(Protocol):
    def extract(self, path: Path) -> ExtractedDocument: ...


class ExtractionReply(BaseModel):
    """The gateway's output contract for an extraction call.

    Field for field :class:`ExtractedDocument`, with one difference that is
    the whole point: ``amount`` is a decimal STRING here. The gateway refuses
    an output model that declares money as ``Decimal`` (pydantic would coerce
    a JSON float silently), so the model answers text and
    :meth:`to_document` re-parses it in code (invariant 2). A test pins the
    field parity, so a new field on either model fails until both carry it.
    """

    doc_type: DocType = "unknown"
    vendor_name: str | None = None
    invoice_number: str | None = None
    amount: DecimalString | None = None  # signed; negative preserved for credits
    invoice_date: str | None = None  # ISO YYYY-MM-DD
    due_date: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    needs_ocr: bool = False
    category: str | None = None

    def to_document(self) -> ExtractedDocument:
        from decimal import Decimal

        data = self.model_dump()
        amount = data.pop("amount")
        return ExtractedDocument(amount=Decimal(amount) if amount is not None else None, **data)


class FixtureExtractor:
    """Deterministic extractor for tests: reads a JSON sidecar per file."""

    def extract(self, path: Path) -> ExtractedDocument:
        sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
        if not sidecar.exists():
            return ExtractedDocument(
                doc_type="unknown",
                confidence=0.0,
                warnings=[f"no extraction sidecar for {path.name}"],
            )
        try:
            return ExtractedDocument.model_validate_json(sidecar.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise ExtractionError(
                f"sidecar for {path.name} failed validation: {exc}",
                cause="bad_reply",
                transient=False,
            ) from exc


def read_document_text(path: Path) -> str:
    """Pull a document's text layer for the model to read alongside the file.

    Text files return directly; PDFs go through ``core.llm.rasterize.pdf_text``
    (the same reader the rasterizer asks before it renders anything, so a file
    can never have a text layer here and none there); image formats have no
    text layer this reader can pull, so they return ``""`` and the file rides
    as an attachment alone. Kept module-level and injectable so tests never
    touch a real file parser.
    """
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from core.llm.rasterize import pdf_text

        return pdf_text(path)
    return ""  # images and everything else: no extractable text layer


class GatewayExtractor:
    """Live extraction through the model seam.

    One call per document: ``complete_for(ctx, job_type, ...)`` resolves the
    tier from the tenant policy, prices it, refuses it past the monthly
    budget, records it in ``llm_calls``, and validates the reply. Everything
    provider-specific (which wire format, which endpoint, whether the file is
    read off disk or inlined) belongs to the adapter the tier names.

    ``tier`` pins a tier by name instead of resolving the job type, which is
    what ``--param extractor=qwen`` and ``--param extractor=tier:<name>``
    select. ``adapter`` overrides the wire for every attempt (tests and evals
    hand in a seeded fixture adapter). ``text_reader`` and ``completer`` are
    injected so tests run with no parser and no model.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        job_type: str = INVOICE_EXTRACT_JOB,
        tier: str | None = None,
        timeout_s: int = 120,
        max_file_bytes: int = MAX_EXTRACT_FILE_BYTES,
        adapter: Any | None = None,
        text_reader: Callable[[Path], str] | None = None,
        completer: Callable[..., Any] | None = None,
    ) -> None:
        self._ctx = ctx
        self._job_type = job_type
        self._tier = tier
        self._timeout_s = timeout_s
        self._max_file_bytes = max_file_bytes
        self._adapter = adapter
        self._read_text = text_reader or read_document_text
        self._complete = completer

    def extract(self, path: Path) -> ExtractedDocument:
        from core.llm.policy import complete_for

        size = path.stat().st_size
        if size > self._max_file_bytes:
            raise ExtractionError(
                f"extraction failed for {path.name}: file is {size} bytes, "
                f"over the {self._max_file_bytes}-byte extraction cap; review manually",
                cause="oversize",
                transient=False,
            )
        text = self._text_layer(path)
        messages = [
            Message("system", _JSON_SPEC),
            Message("user", _TEXT_ASK.format(text=text) if text else _ATTACHED_ASK),
        ]
        call = self._complete or complete_for
        try:
            result = call(
                self._ctx,
                self._job_type,
                messages,
                ExtractionReply,
                attachments=[Attachment(path, document_mime(path))],
                timeout_s=self._timeout_s,
                adapter=self._adapter,
                tier=self._tier,
            )
        except Exception as exc:
            raise self._as_extraction_error(path, exc) from exc
        return result.output.to_document()

    def _text_layer(self, path: Path) -> str:
        """The text layer, or "" when there is none. Reading a text layer is
        new on the DEFAULT path with row 7.10, so a parser that throws must
        degrade to "no text" rather than take a whole intake run down; the
        file still rides as an attachment."""
        try:
            return self._read_text(path).strip()
        except Exception:
            return ""

    def _as_extraction_error(self, path: Path, exc: Exception) -> ExtractionError:
        """Map the seam's failures onto this module's taxonomy, so the retry
        wrapper and the ledger's flag causes keep their meaning."""
        from core.llm import GatewayTransportError, GatewayValidationError
        from core.llm.policy import BudgetExceeded, LlmPolicyError

        if isinstance(exc, GatewayTransportError):
            if exc.cause == "timeout":
                return ExtractionError(
                    f"extraction timed out for {path.name} after {self._timeout_s}s; "
                    f"routing to review instead of blocking intake",
                    cause="timeout",
                    transient=True,
                )
            return ExtractionError(
                f"extraction failed for {path.name}: {exc}",
                cause=exc.cause,
                transient=exc.transient,
            )
        if isinstance(exc, GatewayValidationError):
            return ExtractionError(
                f"extraction reply for {path.name} failed validation",
                cause="bad_reply",
                transient=False,
            )
        if isinstance(exc, BudgetExceeded):
            return ExtractionError(
                f"extraction failed for {path.name}: {exc}",
                cause="budget_exceeded",
                transient=False,
            )
        if isinstance(exc, LlmPolicyError):
            return ExtractionError(
                f"extraction failed for {path.name}: {exc}",
                cause="policy",
                transient=False,
            )
        if isinstance(exc, ExtractionError):
            return exc
        return ExtractionError(
            f"extraction failed for {path.name}: {exc}",
            cause="transport_error",
            transient=True,
        )


class RetryingExtractor:
    """Wrap an extractor and redial transient failures before giving up.

    A real invoice that fails on a one-off transport blip or a momentarily slow
    transport must not be dropped to review on the first miss: D&E 20174 failed
    inside a batch intake yet extracted cleanly standalone (docs/lessons.md,
    "Transient is not terminal"). Only ``transient`` ExtractionErrors are retried; a terminal one
    (oversize, unparseable reply) propagates immediately, since a redial cannot
    change it. The original error, with its ``cause``, is preserved when the
    retries are exhausted.

    ``sleeper`` is injectable so tests do not actually wait. Intake processes
    files one at a time, so there is no concurrency to bound here; the retry is
    the whole fix.
    """

    def __init__(
        self,
        inner: Extractor,
        *,
        attempts: int = 3,
        backoff_s: tuple[float, ...] = (2.0, 5.0),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._attempts = max(attempts, 1)
        self._backoff_s = tuple(backoff_s) or (0.0,)
        self._sleep = sleeper

    def extract(self, path: Path) -> ExtractedDocument:
        for attempt in range(1, self._attempts + 1):
            try:
                return self._inner.extract(path)
            except ExtractionError as exc:
                if not exc.transient or attempt == self._attempts:
                    raise
                delay = self._backoff_s[min(attempt - 1, len(self._backoff_s) - 1)]
                self._sleep(delay)
        raise AssertionError("unreachable: loop returns or raises")  # pragma: no cover


# ---------- the extractor names --------------------------------------------------

FIXTURE = "fixture"
CLAUDE_ALIAS = "claude"
QWEN_ALIAS = "qwen"
TIER_PREFIX = "tier:"
LOCAL_TIER = "local"

# The two provider names are aliases for ONE RELEASE (row 7.10's acceptance):
# `claude` means "whatever tier the policy names for this job", which on the
# live tenant is the seat; `qwen` means the tenant's `local` tier when it has
# one, since that is what the opt-in lane always pointed at. New spellings say
# it directly: `tier:<name>`.
ALIASES = (FIXTURE, CLAUDE_ALIAS, QWEN_ALIAS, f"{TIER_PREFIX}<name>")


def _tier_names(ctx: Any) -> list[str]:
    return sorted(ctx.tenant.llm.tiers)


def _tier_for(ctx: Any, kind: str) -> str | None:
    """The tier an extractor name selects, or ``None`` to let the policy
    resolve the job type. Raises ``ValueError`` on anything unknown."""
    if kind == CLAUDE_ALIAS:
        return None
    if kind == QWEN_ALIAS:
        # The local lane if the tenant still defines one; otherwise fall
        # through to the policy rather than inventing a tier.
        return LOCAL_TIER if LOCAL_TIER in ctx.tenant.llm.tiers else None
    if kind.startswith(TIER_PREFIX):
        name = kind[len(TIER_PREFIX) :]
        if name not in ctx.tenant.llm.tiers:
            raise ValueError(
                f"unknown extractor {kind!r}: [llm.tiers] has no tier {name!r}; "
                f"this tenant names {', '.join(_tier_names(ctx)) or 'no tiers'}"
            )
        return name
    raise ValueError(f"unknown extractor {kind!r}; use one of {', '.join(ALIASES)}")


def resolved_tier(ctx: Any, kind: str, job_type: str = INVOICE_EXTRACT_JOB) -> tuple | None:
    """``(tier, adapter, model)`` for what this extractor name would call, or
    ``None`` for the fixture extractor (which calls no model at all).

    This is what a run key folds in, so the key moves when the resolved tier,
    its adapter, or its model id moves, not merely when the alias changes
    (``docs/run-keys.md``: declare what the run actually depends on).
    """
    from core.llm.policy import LlmPolicyError, resolve

    if kind == FIXTURE:
        return None
    name = _tier_for(ctx, kind)
    if name is not None:
        tier = ctx.tenant.llm.tiers[name]
        return (name, tier.adapter, tier.model)
    try:
        model = resolve(ctx.tenant.llm, job_type)
    except LlmPolicyError as exc:
        # An unresolvable policy is a real input too: the key must change when
        # the tenant fixes it, and the failure itself belongs in the digest.
        return ("unresolved", "unresolved", str(exc))
    return (model.tier, model.adapter, model.model)


def build_extractor(
    kind: str, ctx: Any | None = None, *, job_type: str = INVOICE_EXTRACT_JOB
) -> Extractor:
    """Resolve an extractor by name.

    ``fixture`` is the sidecar extractor (tests, and the only name that needs
    no tenant policy). Every other name is one live :class:`GatewayExtractor`
    wrapped in :class:`RetryingExtractor`, so a transient blip does not cost a
    real invoice; the fixture extractor stays bare, since its failures are
    deterministic and terminal.
    """
    if kind == FIXTURE:
        return FixtureExtractor()
    if ctx is None:
        raise ValueError(
            f"extractor {kind!r} calls a model, so it needs the job's tenant context; "
            f"only {FIXTURE!r} runs without one"
        )
    tier = _tier_for(ctx, kind)
    return RetryingExtractor(GatewayExtractor(ctx, job_type=job_type, tier=tier))
