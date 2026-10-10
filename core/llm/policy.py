"""The tenant model policy (phase 7 row 7.9, docs/model-seam-design.md).

``tenant.toml`` names the models (``[llm.tiers]``), says which tier serves
which job type (``[llm.jobs]``), and caps the month's spend
(``[llm.budget]``). This module is the only place those tables are read:

- :func:`resolve` turns a job type into a :class:`ResolvedModel` (tier,
  adapter name, model id, endpoint, key variable NAME, price, fallbacks).
  A ``deterministic`` job raises :class:`DeterministicJobError`; a job the
  table does not list and no ``default`` covers raises
  :class:`UnresolvedJobError`. Both are config errors naming the job.
- :func:`complete_for` wraps :func:`core.llm.complete` (its signature is
  untouched): it resolves the model, checks the monthly budget, makes the
  call, and writes exactly one ``llm_calls`` row per gateway call through
  :mod:`core.llm.telemetry`. A call past the cap writes a ``budget_refused``
  row and raises :class:`BudgetExceeded` before any adapter is touched;
  the runner turns that row into an ``llm.budget`` anomaly on the run.

Nothing here names a model or a provider: every id and every key variable
comes from the tenant file. The decision behind the table's shape is
``docs/decisions/2026-09-12-llm-policy-table-shape.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from core.engine.config import LLM_DETERMINISTIC, LlmTier
from core.engine.contracts import JobContext
from core.llm import telemetry
from core.llm.gateway import (
    Adapter,
    Attachment,
    GatewayResult,
    GatewayTransportError,
    GatewayValidationError,
    Message,
    Pricing,
    complete,
)

DETERMINISTIC = LLM_DETERMINISTIC
DEFAULT_JOB = "default"

# ---- errors -------------------------------------------------------------------


class LlmPolicyError(ValueError):
    """A tenant policy problem: the job cannot be served as configured."""


class DeterministicJobError(LlmPolicyError):
    """The job is ``deterministic`` in ``[llm.jobs]``: no model may serve it."""

    def __init__(self, job_type: str) -> None:
        super().__init__(
            f"[llm.jobs].{job_type} is {DETERMINISTIC!r}: no model may be called for this job"
        )
        self.job_type = job_type


class UnresolvedJobError(LlmPolicyError):
    """The job is not in ``[llm.jobs]`` and the table has no ``default``."""

    def __init__(self, job_type: str) -> None:
        super().__init__(
            f"[llm.jobs] names no tier for {job_type!r} and has no {DEFAULT_JOB!r}; "
            "add the job to the tenant policy"
        )
        self.job_type = job_type


class DocumentTierRefused(LlmPolicyError):
    """A call carrying a document was routed to a tier the tenant's
    ``[llm].document_tiers`` does not list (#358). Raised before any call;
    a config error, so the job reports it rather than retrying."""

    def __init__(self, job_type: str, tier: str, allowed: list[str]) -> None:
        super().__init__(
            f"{job_type}: tier {tier!r} may not receive documents; [llm].document_tiers "
            f"allows {', '.join(allowed) or 'none'} (route the job to one of them)"
        )
        self.job_type = job_type
        self.tier = tier


# The engine's jobs that send a document to a model, as an attachment. A test
# holds this to the agents' own constants; engine doctor checks their routes.
DOCUMENT_JOB_TYPES = frozenset(
    {"invoice_extract", "receipt_extract", "inbox_classify", "scan_group"}
)


class BudgetExceeded(RuntimeError):
    """The month's recorded spend reached ``[llm.budget].monthly_usd``. No
    call was made; the refusal is on the record. A job catches this and
    routes its item to review the way it routes any extraction failure."""

    def __init__(self, job_type: str, spent: Decimal, cap: Decimal) -> None:
        super().__init__(
            f"{job_type}: the monthly model budget is spent "
            f"({spent} of {cap} USD recorded this month); no call made"
        )
        self.job_type = job_type
        self.spent = spent
        self.cap = cap


# ---- resolution ----------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """A tier as the gateway needs it. ``api_key_env`` is a variable NAME;
    the adapter reads the value at call time."""

    tier: str
    adapter: str
    model: str
    base_url: str
    api_key_env: str
    pricing: Pricing
    fallback: tuple[str, ...] = ()


def _resolved(name: str, tier: LlmTier) -> ResolvedModel:
    return ResolvedModel(
        tier=name,
        adapter=tier.adapter,
        model=tier.model,
        base_url=tier.base_url,
        api_key_env=tier.api_key_env,
        pricing=Pricing(
            input_usd_per_mtok=tier.pricing.input_usd_per_mtok,
            output_usd_per_mtok=tier.pricing.output_usd_per_mtok,
        ),
        fallback=tuple(tier.fallback),
    )


def resolve(settings: Any, job_type: str) -> ResolvedModel:
    """Job type to tier (``[llm.jobs]``, then ``default``), tier to model.

    ``settings`` is the tenant's ``LlmSettings`` (or the runner's traced
    view of it; only attribute reads happen here, so the run-key audit sees
    ``llm.jobs`` and ``llm.tiers``).
    """
    jobs: dict[str, str] = settings.jobs
    tier_name = jobs.get(job_type, jobs.get(DEFAULT_JOB))
    if tier_name is None:
        raise UnresolvedJobError(job_type)
    if tier_name == DETERMINISTIC:
        raise DeterministicJobError(job_type)
    tiers: dict[str, LlmTier] = settings.tiers
    tier = tiers.get(tier_name)
    if tier is None:  # config validation forbids this; belt and braces
        raise LlmPolicyError(f"[llm.jobs].{job_type} names tier {tier_name!r}, not in [llm.tiers]")
    return _resolved(tier_name, tier)


# ---- the model-site aliases (rows 7.10, 7.11) ---------------------------------

FIXTURE_ALIAS = "fixture"
"""The sidecar/deterministic stand-in a site keeps for tests and CI. It calls
no model at all, so it needs no tenant policy."""

POLICY_ALIAS = "claude"
""""Whatever tier the policy names for this job". A provider spelling kept for
one release (the sites' ``--param`` values predate the policy tables)."""

TIER_PREFIX = "tier:"
SITE_ALIASES = (FIXTURE_ALIAS, POLICY_ALIAS, f"{TIER_PREFIX}<name>")


def tier_for_alias(settings: Any, alias: str, *, site: str) -> str | None:
    """The tier a model site's ``--param`` alias names, or ``None`` to let the
    job type resolve through ``[llm.jobs]``.

    ``site`` is the noun the error message uses ("inbox classifier",
    "grouper"), so an unknown value fails naming what is legal FOR THAT SITE.
    Raises :class:`LlmPolicyError` (a ``ValueError``) on anything unknown.
    """
    if alias == POLICY_ALIAS:
        return None
    if alias.startswith(TIER_PREFIX):
        name = alias[len(TIER_PREFIX) :]
        if name not in settings.tiers:
            raise LlmPolicyError(
                f"unknown {site} {alias!r}: [llm.tiers] has no tier {name!r}; "
                f"this tenant names {', '.join(sorted(settings.tiers)) or 'no tiers'}"
            )
        return name
    raise LlmPolicyError(f"unknown {site} {alias!r}; use one of {', '.join(SITE_ALIASES)}")


def resolved_tier_for_alias(
    settings: Any, alias: str, job_type: str, *, site: str
) -> tuple[str, str, str] | None:
    """``(tier, adapter, model)`` for what this alias would call, or ``None``
    for the fixture alias (which calls no model).

    This is what a run key folds in, so the key moves when the resolved tier,
    its adapter, or its model id moves, not merely when the alias changes
    (``docs/run-keys.md``: declare what the run actually depends on).
    """
    if alias == FIXTURE_ALIAS:
        return None
    name = tier_for_alias(settings, alias, site=site)
    if name is not None:
        tier: LlmTier = settings.tiers[name]
        return (name, tier.adapter, tier.model)
    try:
        model = resolve(settings, job_type)
    except LlmPolicyError as exc:
        # An unresolvable policy is a real input too: the key must change when
        # the tenant fixes it, and the failure itself belongs in the digest.
        return ("unresolved", "unresolved", str(exc))
    return (model.tier, model.adapter, model.model)


def _resolve_tier(settings: Any, name: str) -> ResolvedModel:
    tiers: dict[str, LlmTier] = settings.tiers
    tier = tiers.get(name)
    if tier is None:
        raise LlmPolicyError(f"[llm.tiers] has no tier {name!r}")
    return _resolved(name, tier)


# ---- adapters ------------------------------------------------------------------


def _fixture(resolved: ResolvedModel) -> Adapter:
    from core.llm.adapters.fixture import FixtureAdapter

    # A bare fixture tier answers nothing: every call is a non-transient
    # "no fixture reply" transport failure. A test or an eval hands
    # complete_for() its seeded adapter through the ``adapter`` argument.
    return FixtureAdapter({})


def _anthropic(resolved: ResolvedModel) -> Adapter:
    from core.llm.adapters.anthropic_messages import AnthropicMessagesAdapter

    kwargs: dict[str, Any] = {"api_key_env": resolved.api_key_env}
    if resolved.base_url:
        kwargs["endpoint"] = resolved.base_url
    return AnthropicMessagesAdapter(**kwargs)


def _openai_compat(resolved: ResolvedModel) -> Adapter:
    from core.llm.adapters.openai_compat import OpenAICompatAdapter

    return OpenAICompatAdapter(base_url=resolved.base_url, api_key_env=resolved.api_key_env)


def _claude_sdk(resolved: ResolvedModel) -> Adapter:
    from core.llm.adapters.claude_sdk_complete import ClaudeSdkCompleteAdapter

    # A real complete() adapter since row 7.10: the tier that describes the
    # owner's seat can now SERVE a job, at the same flat rate. The SDK itself
    # is the [claude] extra and is imported when the call is made, not here,
    # so a host without it still loads the policy.
    return ClaudeSdkCompleteAdapter()


ADAPTERS: dict[str, Callable[[ResolvedModel], Adapter]] = {
    "fixture": _fixture,
    "anthropic_messages": _anthropic,
    "openai_compat": _openai_compat,
    "claude_agent_sdk": _claude_sdk,
}


def build_adapter(resolved: ResolvedModel) -> Adapter:
    """The adapter a tier names, constructed from the tier's connection
    details. Config validation already limited the name to ``LLM_ADAPTERS``."""
    factory = ADAPTERS.get(resolved.adapter)
    if factory is None:
        raise LlmPolicyError(f"tier {resolved.tier!r}: unknown adapter {resolved.adapter!r}")
    return factory(resolved)


# ---- the policy call --------------------------------------------------------------


WITHHELD_DETAIL = "reply failed validation; detail withheld for this job type"
"""What a ``redact_detail`` job records instead of the failure text. A
pydantic validation error QUOTES the model's reply (``input_value=...``), so a
job whose replies may carry private content (the receipt-inbox classifier's
label-only contract, owner decision 2026-08-12) records THAT the call failed
and never WHAT was said. The status column still says which failure it was."""


def _detail(text: str, redact: bool) -> str:
    return WITHHELD_DETAIL if redact else text


def complete_for[T: BaseModel](
    ctx: JobContext,
    job_type: str,
    messages: list[Message],
    output_model: type[T],
    *,
    attachments: list[Attachment] | None = None,
    timeout_s: int = 120,
    adapter: Adapter | None = None,
    tier: str | None = None,
    redact_detail: bool = False,
    now: datetime | None = None,
) -> GatewayResult[T]:
    """One model job under the tenant policy: resolve, budget, call, record.

    ``adapter`` overrides the tier's adapter for every attempt (tests and
    evals hand in a seeded fixture adapter; the tier's model id and price
    still apply). ``tier`` names a tier DIRECTLY instead of resolving the job
    type through ``[llm.jobs]`` (the AP extractor's ``--param extractor=``
    aliases, row 7.10); a job the table calls ``deterministic`` is still
    refused, because that word outranks any caller. ``redact_detail`` keeps a
    failure's text out of the ``llm_calls`` row for a job whose replies may
    carry private content (:data:`WITHHELD_DETAIL`). ``now`` is the budget
    clock, injectable for tests. Every gateway call writes one ``llm_calls``
    row, ok or failed; a transient transport failure walks the tier's
    ``fallback`` list, one row per attempt. Raises what the gateway raises,
    plus :class:`LlmPolicyError` (config) and :class:`BudgetExceeded` (the
    cap, before any call).
    """
    settings = ctx.tenant.llm
    if tier is None:
        resolved = resolve(settings, job_type)
    else:
        if settings.jobs.get(job_type) == DETERMINISTIC:
            raise DeterministicJobError(job_type)
        resolved = _resolve_tier(settings, tier)
    conn = ctx.ledger.conn
    common = {"tenant": ctx.tenant_slug, "job_type": job_type, "run_key": ctx.run_key}

    allowed = settings.document_tiers
    gated = bool(attachments) and allowed is not None
    if gated and resolved.tier not in allowed:
        refused = DocumentTierRefused(job_type, resolved.tier, list(allowed))
        telemetry.record_call(
            conn,
            **common,
            tier=resolved.tier,
            adapter=resolved.adapter,
            model=resolved.model,
            status=telemetry.STATUS_DOCUMENTS_REFUSED,
            detail=str(refused),
        )
        raise refused

    cap = settings.budget.monthly_usd
    if cap is not None:
        spent = telemetry.month_to_date_usd(conn, ctx.tenant_slug, now=now)
        if spent >= cap:
            refusal = BudgetExceeded(job_type, spent, cap)
            telemetry.record_call(
                conn,
                **common,
                tier=resolved.tier,
                adapter=resolved.adapter,
                model=resolved.model,
                status=telemetry.STATUS_BUDGET_REFUSED,
                detail=str(refusal),
            )
            raise refusal

    chain = [resolved, *(_resolve_tier(settings, name) for name in resolved.fallback)]
    if gated:
        # A fallback the list does not name never sees the document.
        chain = [attempt for attempt in chain if attempt.tier in (allowed or [])]
    for index, attempt in enumerate(chain):
        wire = adapter if adapter is not None else build_adapter(attempt)
        try:
            result = complete(
                job_type,
                messages,
                output_model,
                adapter=wire,
                model=attempt.model,
                attachments=attachments,
                timeout_s=timeout_s,
                pricing=attempt.pricing,
            )
        except GatewayValidationError as exc:
            telemetry.record_call(
                conn,
                **common,
                tier=attempt.tier,
                adapter=wire.name,
                model=attempt.model,
                retries=max(len(exc.replies) - 1, 0),
                status=telemetry.STATUS_VALIDATION_FAILED,
                detail=_detail(exc.errors[-1] if exc.errors else "", redact_detail),
            )
            raise
        except GatewayTransportError as exc:
            telemetry.record_call(
                conn,
                **common,
                tier=attempt.tier,
                adapter=wire.name,
                model=attempt.model,
                status=telemetry.STATUS_TRANSPORT_FAILED,
                detail=f"{exc.cause}: {_detail(str(exc), redact_detail)}",
            )
            if exc.transient and index + 1 < len(chain):
                continue
            raise
        record = result.record
        telemetry.record_call(
            conn,
            **common,
            tier=attempt.tier,
            adapter=record.adapter,
            model=record.model,
            provider_model=record.provider_model,
            tokens_in=record.input_tokens,
            tokens_out=record.output_tokens,
            usd=record.usd if record.usd is not None else Decimal("0"),
            retries=record.retries,
            latency_ms=record.latency_ms,
        )
        return result
    raise AssertionError("unreachable: the chain returns or raises")  # pragma: no cover
