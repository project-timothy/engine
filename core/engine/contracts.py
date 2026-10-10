"""Job interface contracts.

Each agent exposes ``JOBS: dict[str, JobHandler]``. A handler is two pure-ish
functions: ``key(ctx)`` returns the run's deterministic idempotency key
(invariant 3), and ``run(ctx)`` does the work and returns a ``JobOutput``. The
runner owns all persistence, git commits, idempotency short-circuiting, and
``RunResult`` assembly, so agents never touch the ledger's write internals or
re-implement the no-op guarantee.

Splitting ``key`` from ``run`` is deliberate: the runner computes the key and
checks the ledger *before* executing, so an idempotent re-run does no work at
all. ``key`` must therefore be cheap and free of side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..ledger import Ledger
from .config import TenantConfig
from .guard import WriteGuard
from .result import Anomaly, RubricScore


@dataclass
class JobContext:
    """Everything a job is handed to do its work."""

    tenant: TenantConfig
    tenant_slug: str
    ledger: Ledger
    agent: str
    job: str
    shadow: bool = False
    params: dict = field(default_factory=dict)
    agent_dir: Path | None = None
    # Every file write a job performs must pass guard.check_write first.
    guard: WriteGuard = field(default_factory=lambda: WriteGuard([]))
    # The run's idempotency key, set by the runner once computed (empty while
    # the key itself is being built). Namespaces job records.
    run_key: str = ""
    # The tenant's authority.toml as an ``authority_gate.TenantAuthority``,
    # or None when it has none (#435); lanes read it in place of their
    # ``unattended`` lists.
    authority: Any = None

    def record_now(self, key: str, record_type: str, payload: dict) -> bool:
        """Durable AT ONCE, not at return (honesty audit #170, 03-F9): the
        record half of record-then-move. Write the record, then move the
        file; a death between the two leaves the record, so the next run
        finds the move on the record instead of an orphan. Idempotent on
        ``key`` within the run key. Shadow runs record nothing and return
        False. The runner links the record to the run row when the run
        lands, ok or FAILED."""
        if self.shadow or not self.run_key:
            return False
        return self.ledger.record_now(
            idempotency_key=f"{self.run_key}:rec:{key}",
            run_key=self.run_key,
            tenant=self.tenant_slug,
            agent=self.agent,
            job=self.job,
            record_type=record_type,
            payload=payload,
        )

    def records(self, record_type: str) -> list[dict]:
        """Every job record of ``record_type`` this tenant ever wrote (any
        run), newest last: the memory a later run reads to heal a move whose
        event never landed."""
        return self.ledger.job_records(tenant=self.tenant_slug, record_type=record_type)


class EventSpec(BaseModel):
    """An event the job wants recorded. The runner namespaces the idempotency
    key under the run key, so jobs only supply a stable local ``key``."""

    key: str
    event_type: str
    payload: dict = Field(default_factory=dict)


class ApprovalSpec(BaseModel):
    """An action the job is parking for human approval instead of executing."""

    key: str
    action_type: str
    params: dict = Field(default_factory=dict)
    reason: str = ""
    # Keys of this lane's OLDER pending cards that this one replaces (proposal
    # 99e8ae57). The lane only declares; the runner closes each as
    # ``superseded``, same agent and action type, pending rows only.
    supersedes_keys: list[str] = Field(default_factory=list)
    # Set only for a tenant with authority.toml (#435): the lane's agent
    # holds the grant, so the card is written already decided by it, with
    # the evaluator's reason. Empty (always, without the file): it waits.
    decided_by: str = ""
    decided_reason: str = ""


class JobOutput(BaseModel):
    """What a job hands back to the runner. Pure data, no side effects."""

    status: Literal["ok", "needs_approval", "error"] = "ok"
    summary: str = ""
    actions: list[str] = Field(default_factory=list)
    events: list[EventSpec] = Field(default_factory=list)
    approvals: list[ApprovalSpec] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    # A job may self-score the repeatability rubric; the runner passes it
    # through. None keeps the Phase 1 stub on the RunResult.
    rubric: RubricScore | None = None


class RetryPolicy(BaseModel):
    """Bounded cross-run retries with backoff (phase 7 row 7.23).

    Declared on a :class:`JobHandler`; default none, so a job without one
    behaves exactly as before. When a job with a policy fails with a cause
    the policy lists, the runner records the failure as it always has (the
    #172 trace) and schedules the next attempt; ``engine jobs resume``
    executes it when due, as the SAME run under the ORIGINAL run key.

    ``max_attempts`` counts every attempt including the first (so 3 means
    two retries). ``backoff_seconds`` is the wait before attempt 2, 3, ...;
    a ladder shorter than the budget repeats its last rung. ``retry_on``
    names the failure causes that earn a retry, from the taxonomy the
    extractor and the gateway already raise with (``timeout``,
    ``transport_error``, ...); a cause absent from the list, a failure that
    declares itself not transient, and a reported error (``status="error"``
    with no cause, recorded as ``reported``) stay FAILED with no retry.

    This is the cross-run retry. ``RetryingExtractor`` and the gateway's
    tier fallback redial INSIDE one run and are a different mechanism
    (docs/retries.md).
    """

    max_attempts: int = Field(ge=2)
    backoff_seconds: list[int] = Field(min_length=1)
    retry_on: list[str] = Field(min_length=1)

    @field_validator("backoff_seconds")
    @classmethod
    def _non_negative(cls, rungs: list[int]) -> list[int]:
        if any(r < 0 for r in rungs):
            raise ValueError("backoff_seconds must be non-negative")
        return rungs

    def delay_before(self, attempt: int) -> int:
        """Seconds to wait before ``attempt`` (2 or more) may run."""
        return self.backoff_seconds[min(max(attempt - 2, 0), len(self.backoff_seconds) - 1)]

    def retries(self, cause: str) -> bool:
        return cause in self.retry_on


@dataclass(frozen=True)
class JobHandler:
    """A named job: a cheap key function plus the work function, and an
    optional cross-run retry policy (none by default)."""

    key: Callable[[JobContext], str]
    run: Callable[[JobContext], JobOutput]
    retry: RetryPolicy | None = None
