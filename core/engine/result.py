"""The structured result object every run produces.

Orchestration consumes these objects, never prose (architecture 3.4). The
result carries status, the actions taken, items needing approval, anomalies,
and a self-score against the seven-point repeatability rubric. In Phase 1 the
rubric is a stub: the fields exist and carry the contract, but automated
scoring is Phase 5 (decision log, 2026-06-10). Stubbed dimensions are ``None``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

RunStatus = Literal["ok", "needs_approval", "noop", "error"]


class RubricScore(BaseModel):
    """Self-score against the 7-point repeatability rubric.

    Each dimension is 0.0..1.0 once scored, or ``None`` while stubbed. The
    seven names are fixed; they are the contract Phase 5's auto-scorer fills.
    """

    determinism: float | None = None
    input_contract: float | None = None
    human_gates: float | None = None
    failure_modes: float | None = None
    idempotency: float | None = None
    context_footprint: float | None = None
    handoff_quality: float | None = None
    notes: str = "stub: automated rubric scoring lands in phase 5"

    @property
    def is_stub(self) -> bool:
        return all(
            getattr(self, dim) is None
            for dim in (
                "determinism",
                "input_contract",
                "human_gates",
                "failure_modes",
                "idempotency",
                "context_footprint",
                "handoff_quality",
            )
        )


class ApprovalNeeded(BaseModel):
    """An action parked for human approval rather than executed."""

    action_type: str
    params: dict = Field(default_factory=dict)
    reason: str = ""


class Anomaly(BaseModel):
    """Something the run noticed that a human may want to know about."""

    code: str
    detail: str = ""


class LlmTotals(BaseModel):
    """What the run's model calls cost, summed from its ``llm_calls`` rows
    (phase 7 row 7.9). ``calls`` counts every gateway call made (ok or
    failed); a budget refusal is not a call. ``usd`` is a Decimal and lands
    in the stored JSON as a string, never a float."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: Decimal = Decimal("0")


class RunResult(BaseModel):
    """The full, structured outcome of a single run."""

    tenant: str
    agent: str
    job: str
    status: RunStatus
    shadow: bool = False
    idempotency_key: str
    actions: list[str] = Field(default_factory=list)
    approvals_needed: list[ApprovalNeeded] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    rubric: RubricScore = Field(default_factory=RubricScore)
    summary: str = ""
    commit: str | None = None
    # The run's model-call totals (row 7.9); zeros when no model was called.
    llm: LlmTotals = Field(default_factory=LlmTotals)
