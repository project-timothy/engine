"""The counsel drafter: a model reasons over computed facts, and only that.

The boundary is structural, not aspirational: the call carries the facts JSON
in the prompt, allows NO tools, and runs one turn, so the model cannot read
fresh data even if it tries. Its entire output is a prose string; the runner
puts that string in one report section and nowhere else, so advice can never
mint a checklist item or a severity. When the model is unavailable (or
--local-only), the deterministic fallback renders the same facts as plain
observations: degraded voice, same truth, and the report says which voice
spoke.

Which model answers is tenant policy, not a constant here (row 7.12): the
tenant's ``[llm.jobs].draft_advisory`` names a tier in ``[llm.tiers]`` and the
vendored client in :mod:`auditor.advisory.llm_client` speaks to it. A tenant
naming no ``[llm]`` tables keeps the Claude Agent SDK path under the owner's
seat, which is what this drafter has always run.
"""

from __future__ import annotations

import json
from typing import Any

from .llm_client import ADVISORY_JOB, SdkMissing, complete_text

__all__ = [
    "ADVISORY_JOB",
    "DRAFT_TIMEOUT_S",
    "FACTS_TURN",
    "PROMPT",
    "SYSTEM_PROMPT",
    "SdkMissing",
    "draft_counsel",
    "facts_turn",
    "fallback_counsel",
]

DRAFT_TIMEOUT_S = 180

SYSTEM_PROMPT = """You are drafting the Advisory section of a small business's nightly
bookkeeping audit report. You are given COMPUTED FACTS as JSON. All money
values are integer cents.

Write brief, plain, professional counsel — a CPA's eye between visits, not
an alarm bell. Rules, all hard:
- Use ONLY the facts given. Never invent, estimate, or extrapolate a number.
  Render cents as dollars (e.g. 123456 -> $1,234.56).
- Comment only where something is worth the owner's attention: coding drift,
  aging worth watching, treatment questions to settle, close readiness. If a
  topic's facts are unremarkable, say nothing about it.
- No headings, no bullets-for-everything: 1-3 short paragraphs of counsel.
  If genuinely nothing merits comment, reply exactly: All quiet in the books.
- Whether a taxpayer is due a 1099 is `form_1099_due`, stated in words by
  `form_1099_note`, and nothing else. Never infer it from `w9_on_file`
  (which says whether a FORM is on file, not whether a filing is owed) or
  from `tax_classification`. Quote the note or stay silent about it.
- Never propose creating, changing, or closing audit findings; never assign
  severities; never instruct writing to any system. Observations and
  suggestions to a human owner only."""

FACTS_TURN = "FACTS:\n{facts}"
"""The one user turn: the computed facts and nothing else."""

PROMPT = f"{SYSTEM_PROMPT}\n\n{FACTS_TURN}\n"
"""The two turns as one string, for the seat path (which has no system slot).
Kept byte-identical to the prompt this drafter sent before row 7.12 split it."""


def facts_turn(facts: dict) -> str:
    """The facts as the model sees them: sorted keys, one-space indent, so the
    same books produce the same prompt."""
    return FACTS_TURN.format(facts=json.dumps(facts, indent=1, sort_keys=True))


def draft_counsel(
    facts: dict,
    *,
    llm: dict[str, Any] | None = None,
    timeout_s: int = DRAFT_TIMEOUT_S,
    client: Any | None = None,
) -> str:
    """One no-tools, single-turn model call through the tenant's policy.

    ``llm`` is the tenant's raw ``[llm]`` table (``AuditorTenantConfig.raw``);
    ``client`` overrides the tier's client for an eval. Raises on any failure:
    the caller falls back to the deterministic voice and names the reason.
    """
    return complete_text(
        llm,
        ADVISORY_JOB,
        system=SYSTEM_PROMPT,
        user=facts_turn(facts),
        timeout_s=timeout_s,
        client=client,
    )


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def fallback_counsel(facts: dict) -> str:
    """The same facts as plain deterministic observations — the voice when
    no model is in the loop (--local-only, or the drafter failed)."""
    lines: list[str] = []
    drift = facts.get("coding-drift", {}).get("vendors_coded_multiple_ways", {})
    if drift:
        names = ", ".join(sorted(drift))
        lines.append(f"Coding drift: {len(drift)} vendor(s) coded more than one way ({names}).")
    aging = facts.get("aging", {})
    oldest = aging.get("oldest")
    if oldest:
        lines.append(
            f"Aging: {aging.get('open_payables', 0)} open payable(s); oldest is "
            f"{oldest['vendor']} / {oldest['invoice_number']} at {oldest['age_days']} days "
            f"({_dollars(oldest['cents'])})."
        )
    treatment = facts.get("treatment-questions", {}).get("reimbursement_vendors", [])
    if treatment:
        total = sum(t["cents"] for t in treatment)
        lines.append(
            f"Treatment: {len(treatment)} reimbursement-shaped vendor(s) totalling "
            f"{_dollars(total)} — confirm the reimbursement-vs-loans treatment reads "
            "consistently."
        )
    readiness = facts.get("close-readiness", {})
    committed = readiness.get("committed_not_cleared", {})
    if committed.get("count"):
        lines.append(
            f"Close readiness: {committed['count']} committed payment(s) "
            f"({_dollars(committed['cents'])}) not yet cleared."
        )
    return " ".join(lines) if lines else "All quiet in the books."
