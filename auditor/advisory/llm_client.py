"""The auditor's own minimal copy of the model seam (phase 7 row 7.12).

``auditor/`` imports nothing from ``core/`` (``auditor.evals.independence_lint``,
auditor design principle 2), so the drafter cannot call ``core.llm``. This
module is the deliberate small copy the seam design names
(``docs/model-seam-design.md``, "The auditor independence rule"): the tenant's
own ``[llm.tiers]`` / ``[llm.jobs]`` tables, resolved for one job, spoken to
over one of the adapter names the engine's config validates. A parity test on
the engine side (``tests/unit/test_auditor_advisory_gateway.py``) fails the
suite if the two copies disagree about an adapter name, an endpoint, or the
reserved ``deterministic`` word.

Three ways it is smaller than ``core.llm``, each on purpose:

- the reply is PROSE, not a validated pydantic model. The advisory contract
  is one string in one report section (``advisory/__init__.py``), so there is
  no output schema to validate and no money field to protect: the money rule
  the gateway enforces has nothing to enforce here;
- no retry and no tier ``fallback`` walk. One call; a failure degrades to the
  deterministic fallback voice, which is a better answer than a second model;
- no telemetry row. The auditor's store has no calls table and it reads the
  engine's ledger read-only, so a call is not recorded (decision
  ``docs/decisions/2026-09-15-advisory-drafter-vendored-seam.md``).

Everything else follows the engine: the API key comes from the environment
variable NAMED by the tier (``api_key_env``); the name is tenant config, the
value never is. No model id and no tenant name lives here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ADVISORY_JOB = "draft_advisory"
"""The ``[llm.jobs]`` key this module resolves. The tenant names the tier."""

DETERMINISTIC = "deterministic"
DEFAULT_JOB = "default"

ADAPTER_FIXTURE = "fixture"
ADAPTER_ANTHROPIC = "anthropic_messages"
ADAPTER_OPENAI = "openai_compat"
ADAPTER_SEAT = "claude_agent_sdk"
ADAPTERS = (ADAPTER_FIXTURE, ADAPTER_ANTHROPIC, ADAPTER_OPENAI, ADAPTER_SEAT)
"""The adapter names a tier may carry, in the engine's order. Parity-tested
against ``core.engine.config.LLM_ADAPTERS``."""

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 1024
NO_AUTH_PLACEHOLDER = "sk-no-auth"

SDK_NEEDS_THE_EXTRA = (
    "the advisory drafter needs the Claude Agent SDK, which is not installed; "
    "install it with the [claude] extra: uv sync --extra claude"
)


# ---- errors -------------------------------------------------------------------


class AdvisoryLlmError(RuntimeError):
    """Base of every failure this module raises. The runner catches it (and
    anything else) and renders the deterministic fallback voice."""


class PolicyError(AdvisoryLlmError):
    """A tenant config problem: the job cannot be served as configured."""


class TransportError(AdvisoryLlmError):
    """The provider could not be reached or would not answer. ``cause`` is a
    short label a report may print: ``timeout``, ``transport_error``,
    ``no_api_key``, ``refusal``, ``empty_reply``, ``fixture``."""

    def __init__(self, message: str, *, cause: str = "transport_error") -> None:
        super().__init__(message)
        self.cause = cause


class SdkMissing(ModuleNotFoundError):
    """The Claude Agent SDK (the ``[claude]`` extra, row 7.15) is absent. A
    ``ModuleNotFoundError`` on purpose, the same shape the engine's seam
    raises, so a caller testing for a missing module still sees one."""


def failure_label(exc: BaseException) -> str:
    """One short word for why the counsel is the fallback voice. Never the
    exception text: a report prints this, and a message could carry an
    endpoint or a provider's own words."""
    if isinstance(exc, TransportError):
        return exc.cause
    if isinstance(exc, SdkMissing):
        return "sdk_missing"
    if isinstance(exc, PolicyError):
        return "policy"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "drafter_error"


# ---- the policy ---------------------------------------------------------------


@dataclass(frozen=True)
class Tier:
    """One ``[llm.tiers]`` entry, only the fields this client needs.
    ``api_key_env`` is a variable NAME; the value is read at call time."""

    name: str
    adapter: str
    model: str
    base_url: str = ""
    api_key_env: str = ""


UNCONFIGURED = Tier(name="unconfigured", adapter=ADAPTER_SEAT, model="default")
"""A tenant with no ``[llm]`` tables at all keeps what the auditor has always
run: the Claude Agent SDK under the owner's seat, model unset. Naming the
tables governs; naming nothing changes nothing."""


def resolve(llm: dict[str, Any] | None, job_type: str) -> Tier:
    """Job type to tier: ``[llm.jobs]``, then ``[llm.jobs].default``.

    ``llm`` is the raw ``[llm]`` table as the auditor's own TOML reader loaded
    it (``AuditorTenantConfig.raw``). Raises :class:`PolicyError` naming the
    job for an unlisted job with no default, for a ``deterministic`` job, and
    for a tier or adapter name the table does not define.
    """
    table = llm or {}
    jobs = table.get("jobs") or {}
    tiers = table.get("tiers") or {}
    if not jobs and not tiers:
        return UNCONFIGURED
    name = jobs.get(job_type, jobs.get(DEFAULT_JOB))
    if name is None:
        raise PolicyError(
            f"[llm.jobs] names no tier for {job_type!r} and has no {DEFAULT_JOB!r}; "
            "add the job to the tenant policy"
        )
    if name == DETERMINISTIC:
        raise PolicyError(
            f"[llm.jobs].{job_type} is {DETERMINISTIC!r}: no model may be called for this job"
        )
    entry = tiers.get(name)
    if entry is None:
        raise PolicyError(f"[llm.jobs].{job_type} names tier {name!r}, not in [llm.tiers]")
    adapter = str(entry.get("adapter", ""))
    if adapter not in ADAPTERS:
        raise PolicyError(
            f"[llm.tiers].{name} names adapter {adapter!r}, not one of {', '.join(ADAPTERS)}"
        )
    return Tier(
        name=str(name),
        adapter=adapter,
        model=str(entry.get("model", "")),
        base_url=str(entry.get("base_url", "")),
        api_key_env=str(entry.get("api_key_env", "")),
    )


# ---- the prompt ---------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    """One advisory call. Two turns and nothing else: the rules as the system
    turn, the computed facts as the single user turn. No tools, no
    attachments, no history, so the model cannot read fresh data even if it
    tries."""

    job_type: str
    model: str
    system: str
    user: str
    timeout_s: int

    def joined(self) -> str:
        """The two turns as one string, for a client with no system slot."""
        return f"{self.system}\n\n{self.user}\n"


class Client(Protocol):
    """One method: a prompt in, the reply text out. Raise
    :class:`TransportError` on anything the provider did or did not do."""

    name: str

    def complete(self, prompt: Prompt) -> str: ...


# ---- the clients --------------------------------------------------------------


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: int) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
        raise TransportError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        raise TransportError(
            f"the model endpoint is unreachable: {exc.reason}",
            cause="timeout" if timed_out else "transport_error",
        ) from exc
    except TimeoutError as exc:
        raise TransportError("the model call timed out", cause="timeout") from exc


def _key(api_key_env: str) -> str | None:
    return os.environ.get(api_key_env) if api_key_env else None


class AnthropicMessagesClient:
    """The Messages API over stdlib ``urllib``. Prose out, so no
    ``output_config`` block and no schema: the reply is the first text block."""

    name = ADAPTER_ANTHROPIC

    def __init__(self, tier: Tier, *, endpoint: str = "") -> None:
        self._tier = tier
        self._endpoint = endpoint or tier.base_url or ANTHROPIC_ENDPOINT

    def complete(self, prompt: Prompt) -> str:
        api_key = _key(self._tier.api_key_env)
        if not api_key:
            raise TransportError(
                f"environment variable {self._tier.api_key_env or '(unnamed)'} is not set; "
                "no key, no call",
                cause="no_api_key",
            )
        body = {
            "model": prompt.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "temperature": 0,
            "system": prompt.system,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt.user}]}],
        }
        payload = _post_json(
            self._endpoint,
            body,
            {"x-api-key": api_key, "anthropic-version": ANTHROPIC_API_VERSION},
            prompt.timeout_s,
        )
        if payload.get("stop_reason") == "refusal":
            raise TransportError("the model declined the request", cause="refusal")
        return "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )


class OpenAICompatClient:
    """Chat completions over stdlib ``urllib``: OpenAI, a compatibility
    endpoint, or the local gateway. No key needed for a local endpoint, the
    same placeholder the engine's adapter uses."""

    name = ADAPTER_OPENAI

    def __init__(self, tier: Tier) -> None:
        self._tier = tier

    def complete(self, prompt: Prompt) -> str:
        base = (self._tier.base_url or "").rstrip("/")
        if not base:
            raise PolicyError(f"[llm.tiers].{self._tier.name} needs a base_url for {self.name}")
        body = {
            "model": prompt.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "temperature": 0,
        }
        key = _key(self._tier.api_key_env) or NO_AUTH_PLACEHOLDER
        payload = _post_json(
            f"{base}/chat/completions", body, {"Authorization": f"Bearer {key}"}, prompt.timeout_s
        )
        choices = payload.get("choices") or []
        if not choices:
            raise TransportError("the endpoint returned no choices", cause="empty_reply")
        return str((choices[0].get("message") or {}).get("content") or "")


class SeatClient:
    """The Claude Agent SDK under the owner's seat: what every live model site
    runs today. The tier's model id is NOT passed through; this path leaves the
    binary on its own default, the way the drafter always has (an SDK adapter
    that honours a model id is row 7.15's call). One turn, zero tools."""

    name = ADAPTER_SEAT

    def __init__(self, tier: Tier) -> None:
        self._tier = tier

    def complete(self, prompt: Prompt) -> str:
        import asyncio

        async def _run() -> str:
            try:
                from claude_agent_sdk import ClaudeAgentOptions, query
            except ModuleNotFoundError as exc:
                raise SdkMissing(SDK_NEEDS_THE_EXTRA) from exc

            options = ClaudeAgentOptions(allowed_tools=[], max_turns=1)
            chunks: list[str] = []
            async for message in query(prompt=prompt.joined(), options=options):
                result = getattr(message, "result", None)
                if isinstance(result, str):
                    chunks.append(result)
            return "\n".join(chunks)

        async def _within_timeout() -> str:
            return await asyncio.wait_for(_run(), prompt.timeout_s)

        return asyncio.run(_within_timeout())


class FixtureClient:
    """Canned prose, no network: what an eval hands in. A bare one answers
    nothing, so a tenant that points a live job at the fixture adapter fails
    loudly instead of drafting silence."""

    name = ADAPTER_FIXTURE

    def __init__(self, reply: str = "") -> None:
        self._reply = reply
        self.prompts: list[Prompt] = []

    def complete(self, prompt: Prompt) -> str:
        self.prompts.append(prompt)
        if not self._reply:
            raise TransportError("the fixture client has no seeded reply", cause="fixture")
        return self._reply


def build_client(tier: Tier) -> Client:
    """The client a tier names. :func:`resolve` already limited the name."""
    if tier.adapter == ADAPTER_ANTHROPIC:
        return AnthropicMessagesClient(tier)
    if tier.adapter == ADAPTER_OPENAI:
        return OpenAICompatClient(tier)
    if tier.adapter == ADAPTER_SEAT:
        return SeatClient(tier)
    if tier.adapter == ADAPTER_FIXTURE:
        return FixtureClient()
    raise PolicyError(f"tier {tier.name!r}: unknown adapter {tier.adapter!r}")


# ---- the call -----------------------------------------------------------------


def complete_text(
    llm: dict[str, Any] | None,
    job_type: str,
    *,
    system: str,
    user: str,
    timeout_s: int,
    client: Client | None = None,
) -> str:
    """Resolve the job, call once, return the model's prose.

    ``client`` overrides the tier's client (an eval hands in a seeded fixture);
    the tier's model id still travels. Raises :class:`PolicyError` or
    :class:`TransportError`, or :class:`SdkMissing` on the seat path; the
    caller degrades to the deterministic fallback voice.
    """
    tier = resolve(llm, job_type)
    wire = client if client is not None else build_client(tier)
    prompt = Prompt(
        job_type=job_type, model=tier.model, system=system, user=user, timeout_s=timeout_s
    )
    text = wire.complete(prompt).strip()
    if not text:
        raise TransportError(f"{job_type}: the model returned no text", cause="empty_reply")
    return text
