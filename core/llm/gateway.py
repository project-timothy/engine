"""The model gateway: one call, one validated pydantic instance, one record.

Every model call in the engine goes through :func:`complete` (design:
``docs/model-seam-design.md``; decision
``docs/decisions/2026-09-11-model-gateway-interface.md``). The gateway owns
the parts that must be identical whatever provider answers:

- the reply contract is a pydantic model, and validation is the boundary
  (invariant 2): a reply that does not validate never reaches a caller;
- one retry with the validation error appended, then
  :class:`GatewayValidationError` carrying both raw replies;
- money is a Decimal STRING in every output schema (:data:`DecimalString`);
  an output model that declares a ``Decimal`` field is refused up front,
  because a JSON number would be coerced silently and the seam would have
  returned a float for money;
- one :class:`CallRecord` per call, so the caller can persist what the call
  cost (the ``llm_calls`` table is row 7.9).

An :class:`Adapter` does only the provider-specific part: turn a
:class:`PromptBundle` plus a JSON schema into raw text and token usage. The
adapters under ``core/llm/adapters/`` are the only places that know a wire
format. No model name lives anywhere under ``core/llm``: the tenant policy
names models (row 7.9) and the caller passes the resolved id in.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, get_args, get_origin

from pydantic import AfterValidator, BaseModel, ValidationError

Role = Literal["system", "user", "assistant"]

# ---- the money rule ----------------------------------------------------------


def _validate_decimal_string(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{value!r} is not a finite decimal string")
    return value


DecimalString = Annotated[str, AfterValidator(_validate_decimal_string)]
"""A money field in an output schema: the model returns text such as
``"1875.00"``, pydantic rejects a JSON number outright (a ``str`` field never
coerces from a number), and the CALLER re-parses with ``Decimal(...)`` in
code. The gateway never hands a float to anyone for money."""


# ---- the prompt side ----------------------------------------------------------


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Attachment:
    """A file for the model to look at. The ADAPTER reads the bytes (base64
    inline on the wire); the gateway only carries the path and the MIME type.
    Images and PDFs are the shapes every adapter accepts."""

    path: Path
    mime: str


@dataclass(frozen=True)
class PromptBundle:
    """Everything an adapter needs for one provider call. ``messages`` may
    hold system turns anywhere; each adapter folds them into its provider's
    system slot. Attachments ride the FIRST user turn (the ask), which keeps
    them in place when the retry appends the correction turns."""

    job_type: str
    model: str
    messages: tuple[Message, ...]
    attachments: tuple[Attachment, ...]
    timeout_s: int

    def system_text(self) -> str:
        return "\n\n".join(m.content for m in self.messages if m.role == "system")

    def turns(self) -> tuple[Message, ...]:
        return tuple(m for m in self.messages if m.role != "system")


# ---- the reply side -----------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class RawReply:
    """What an adapter returns: the reply text as the provider gave it, the
    usage the provider reported, and the model id the provider reports back
    (``None`` when it reports nothing)."""

    text: str
    usage: Usage = Usage()
    model: str | None = None


class Adapter(Protocol):
    """One method: the prompt bundle plus the JSON schema in, raw text and
    usage out. Raise anything on a transport failure; the gateway maps it."""

    name: str

    def complete(self, bundle: PromptBundle, schema: dict[str, Any]) -> RawReply: ...


@dataclass(frozen=True)
class Pricing:
    """List price per million tokens, as Decimals. Comes from the tenant
    policy (row 7.9); the gateway never guesses a price."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal

    def usd(self, usage: Usage) -> Decimal:
        total = (
            Decimal(usage.input_tokens) * self.input_usd_per_mtok
            + Decimal(usage.output_tokens) * self.output_usd_per_mtok
        )
        return total / Decimal(1_000_000)


@dataclass(frozen=True)
class CallRecord:
    """Telemetry for one gateway call, summed over its attempts. The caller
    persists it (the ``llm_calls`` table, row 7.9); the gateway never writes
    to the ledger."""

    job_type: str
    adapter: str
    model: str
    provider_model: str | None
    input_tokens: int
    output_tokens: int
    usd: Decimal | None
    retries: int
    latency_ms: int


@dataclass(frozen=True)
class GatewayResult[T: BaseModel]:
    output: T
    record: CallRecord


# ---- errors -------------------------------------------------------------------


class GatewayError(RuntimeError):
    """Base of every gateway failure."""


class GatewaySchemaError(GatewayError):
    """The output model breaks a seam rule (a ``Decimal`` field for money)."""


class GatewayValidationError(GatewayError):
    """Both replies failed validation. Carries every raw reply and the
    matching error text so a review card can show what the model said."""

    def __init__(self, job_type: str, replies: list[str], errors: list[str]) -> None:
        super().__init__(
            f"{job_type}: the model reply failed validation {len(replies)} time(s); "
            f"last error: {errors[-1] if errors else 'none'}"
        )
        self.job_type = job_type
        self.replies = list(replies)
        self.errors = list(errors)


class GatewayTransportError(GatewayError):
    """The provider could not be reached or would not answer. ``cause`` is a
    short label (``timeout`` / ``transport_error`` / ``no_api_key`` /
    ``refusal``); ``transient`` says whether a redial could plausibly help,
    the same taxonomy the AP extractor's retry wrapper already reads."""

    def __init__(self, message: str, *, cause: str = "transport_error", transient: bool = True):
        super().__init__(message)
        self.cause = cause
        self.transient = transient


# ---- the schema rule ----------------------------------------------------------


def _unwrap(annotation: Any) -> Iterable[Any]:
    origin = get_origin(annotation)
    if origin is Annotated:
        yield from _unwrap(get_args(annotation)[0])
        return
    if origin is not None:
        for arg in get_args(annotation):
            if arg is type(None) or arg is Ellipsis:
                continue
            yield from _unwrap(arg)
        return
    yield annotation


def _check_no_decimal_fields(model: type[BaseModel], path: str, seen: set[type]) -> None:
    if model in seen:
        return
    seen.add(model)
    for name, field in model.model_fields.items():
        for leaf in _unwrap(field.annotation):
            if leaf is Decimal:
                raise GatewaySchemaError(
                    f"{path}{name}: output schemas carry money as DecimalString "
                    "(a decimal STRING re-parsed in code), never as Decimal; a JSON "
                    "number would be coerced silently"
                )
            if isinstance(leaf, type) and issubclass(leaf, BaseModel):
                _check_no_decimal_fields(leaf, f"{path}{name}.", seen)


# ---- the call -----------------------------------------------------------------

_SCHEMA_INSTRUCTION = (
    "Reply with ONLY a single JSON object (no prose, no code fences) that "
    "validates against this JSON schema:\n{schema}"
)

_RETRY_INSTRUCTION = (
    "Your previous reply failed validation against the required JSON schema:\n"
    "{error}\n"
    "Reply again with ONLY the corrected JSON object, no prose."
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _parse_reply[T: BaseModel](raw: str, output_model: type[T]) -> T:
    match = _JSON_OBJECT.search(raw)
    if not match:
        raise ValueError("no JSON object found in the reply")
    data = json.loads(match.group(0))
    return output_model.model_validate(data)


def complete[T: BaseModel](
    job_type: str,
    messages: list[Message],
    output_model: type[T],
    *,
    adapter: Adapter,
    model: str,
    attachments: list[Attachment] | None = None,
    timeout_s: int = 120,
    pricing: Pricing | None = None,
) -> GatewayResult[T]:
    """Run one model job and return the validated output plus its record.

    ``model`` is the provider's model id as resolved by the tenant policy;
    the gateway never chooses it. The JSON schema of ``output_model`` goes to
    the adapter (providers with constrained decoding use it structurally) AND
    into a system turn (every provider sees it as text). Raises
    :class:`GatewaySchemaError`, :class:`GatewayTransportError`, or
    :class:`GatewayValidationError`; nothing else escapes.
    """
    _check_no_decimal_fields(output_model, "", set())
    schema = output_model.model_json_schema()
    history: tuple[Message, ...] = (
        *messages,
        Message("system", _SCHEMA_INSTRUCTION.format(schema=json.dumps(schema, sort_keys=True))),
    )
    files = tuple(attachments or ())
    replies: list[str] = []
    errors: list[str] = []
    input_tokens = output_tokens = 0
    provider_model: str | None = None
    started = time.perf_counter()

    for attempt in range(2):
        bundle = PromptBundle(job_type, model, history, files, timeout_s)
        try:
            raw = adapter.complete(bundle, schema)
        except GatewayError:
            raise
        except TimeoutError as exc:
            raise GatewayTransportError(
                f"{job_type}: the model call timed out after {timeout_s}s",
                cause="timeout",
                transient=True,
            ) from exc
        except Exception as exc:
            raise GatewayTransportError(
                f"{job_type}: model transport failed: {exc}",
                cause="transport_error",
                transient=True,
            ) from exc
        input_tokens += raw.usage.input_tokens
        output_tokens += raw.usage.output_tokens
        provider_model = raw.model or provider_model
        try:
            output = _parse_reply(raw.text, output_model)
        except (ValueError, ValidationError) as exc:
            # json.JSONDecodeError is a ValueError. Record the reply and the
            # error, hand both back to the model once, then give up.
            replies.append(raw.text)
            errors.append(str(exc))
            history = (
                *history,
                Message("assistant", raw.text),
                Message("user", _RETRY_INSTRUCTION.format(error=exc)),
            )
            continue
        usage = Usage(input_tokens, output_tokens)
        record = CallRecord(
            job_type=job_type,
            adapter=adapter.name,
            model=model,
            provider_model=provider_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=pricing.usd(usage) if pricing else None,
            retries=attempt,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return GatewayResult(output=output, record=record)

    raise GatewayValidationError(job_type, replies, errors)
