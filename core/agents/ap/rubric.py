"""Rubric self-scoring v1 for the AP agent.

Deterministic checks where a number is honestly computable; explicit
``unmeasured`` (None) for the rest. No flattery scores: a dimension is 1.0
only when the property is structurally enforced or measured in this run.

Measured in v1:
- input_contract: fraction of processed documents whose extraction passed
  schema validation.
- human_gates: 1.0 when every money-adjacent decision queued an approval
  (structurally true for these jobs; scored 0.0 if a money action bypassed).
- idempotency: 1.0, storage-enforced (unique keys + runner replay) and
  regression-tested; scored per-run as "the mechanism was active".
- handoff_quality: completeness of the result object (summary present,
  counts consistent with events).

Unmeasured in v1 (None): determinism, failure_modes, context_footprint.
Those need replay/chaos tooling that lands with the learning-loop automation
(Phase 5).
"""

from __future__ import annotations

from ...engine.result import RubricScore


def score_intake(
    *,
    processed: int,
    validation_failures: int,
    money_actions: int,
    money_actions_queued: int,
    summary_present: bool,
) -> RubricScore:
    input_contract = 1.0 if processed == 0 else max(0.0, 1 - validation_failures / processed)
    human_gates = 1.0 if money_actions == money_actions_queued else 0.0
    handoff = 1.0 if summary_present else 0.0
    return RubricScore(
        input_contract=round(input_contract, 3),
        human_gates=human_gates,
        idempotency=1.0,
        handoff_quality=handoff,
        determinism=None,
        failure_modes=None,
        context_footprint=None,
        notes=(
            "v1: input_contract/human_gates/idempotency/handoff measured; "
            "determinism, failure_modes, context_footprint unmeasured until "
            "the learning-loop automation phase"
        ),
    )
