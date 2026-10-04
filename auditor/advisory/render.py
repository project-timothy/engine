"""Render the Advisory section: counsel prose plus the deterministic
year-end-CPA appendix. The appendix is facts, always rendered the same way
whether or not a model drafted the counsel above it.

Owner decision 2026-08-22: the appendix is a change report, not a daily
recitation. It prints in full when its structure moved (a taxpayer, W-9
state, classification, or recipient — never amounts); otherwise one quiet
line stands in, and any taxpayer with a W-9 gap keeps its full bullet
nightly until cleared. The runner decides "changed" against the auditor
store's remembered fingerprint and passes it here.
"""

from __future__ import annotations

from .facts import w9_exempt_without_form, w9_gap

FALLBACK_NOTICE = "Counsel above is the deterministic fallback voice"
"""The report's own words when no model drafted the counsel. An honesty rule
(audit 2026-09-03): a degraded voice says so, rather than reading like
counsel a model wrote."""

LOCAL_ONLY_REASON = "local-only"
"""The reason label for a run that was told not to call a model at all."""


def fallback_notice(reason: str) -> str:
    """One line, no secrets and no stack trace: the fact and a short label."""
    return f"({FALLBACK_NOTICE}; no model drafted it. Reason: {reason}.)"


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _floor_note(cpa: dict) -> str:
    """Names the 1099 reporting floor this appendix judged payments against
    (issue #371: the floor is a tax-year fact, not a constant, so the
    report always states which year and which figure were in effect)."""
    floor = cpa.get("floor_cents")
    if floor is None:
        return ""
    year = cpa.get("tax_year")
    year_note = f" tax year {year}" if year else ""
    fallback_note = (
        " (no published figure for this year yet; using the latest known year's floor)"
        if cpa.get("floor_is_fallback")
        else ""
    )
    return f"- 1099-NEC/1099-MISC reporting floor for{year_note}: {_dollars(floor)}{fallback_note}"


def _candidate_line(c: dict) -> str:
    w9 = c["w9_on_file"]
    w9_note = (
        "W-9 on file" if w9 is True else ("W-9 MISSING" if w9 is False else "W-9 status unknown")
    )
    entity = f", tax entity {c['tax_entity']}" if c["tax_entity"] else ""
    recipient = c.get("tax_recipient", "")
    recipient_note = f" (1099 recipient: {recipient})" if recipient else ""
    exempt_note = (
        f" — {c.get('tax_classification') or 'corp'}, no 1099-NEC due"
        + (" (classification per owner; no W-9 on file)" if w9 is not True else "")
        if not c.get("form_1099_due", True)
        else ""
    )
    unregistered_note = (
        ""
        if c.get("registered", True)
        else " — NOT IN vendors.toml (onboard the vendor: "
        "registry entry + W-9; the 1099 classification is unknown until then)"
    )
    return (
        f"- {c['vendor']}{recipient_note}: {_dollars(c['paid_cents'])} paid across "
        f"{c['payments']} payment(s) ({c['cost_type']}{entity}) — {w9_note}"
        f"{exempt_note}{unregistered_note}"
    )


def _w9_clause(candidates: list[dict], needs_action: list[dict]) -> str:
    """The unchanged-night summary's W-9 clause, saying only what it measured.

    What the quiet line can see is the nag list: taxpayers DUE a 1099-NEC
    with no form (``w9_gap``). An exempt corporation without a form is
    deliberately NOT on that list -- owner decision 2026-08-26, the
    classification is his attestation until a form arrives -- so an empty nag
    list is not the same fact as "every taxpayer has a form", and spelling it
    that way asserted something nobody measured on exactly the nights the
    full list that would have corrected it was suppressed (2026-10-02).

    So the cheerful sentence survives only for the night it is true, the
    narrower claim is made when it is the true one, and the exempt shortfall
    is COUNTED rather than named: counting is not nagging, and the owner's
    decision was about the nagging.
    """
    if needs_action:
        return "; W-9 gaps below"
    exempt_gaps = sum(1 for c in candidates if w9_exempt_without_form(c))
    if not exempt_gaps:
        return ", all W-9s on file"
    return (
        ", every taxpayer due a 1099-NEC has a W-9 on file; "
        f"{exempt_gaps} exempt taxpayer(s) have none on file (classification per owner)"
    )


def render_advisory(
    facts: dict,
    counsel: str,
    *,
    appendix_changed: bool = True,
    fallback_reason: str | None = None,
) -> str:
    lines = [counsel.strip() or "All quiet in the books."]
    if fallback_reason:
        lines.append("")
        lines.append(fallback_notice(fallback_reason))

    cpa = facts.get("year-end-cpa")
    if cpa:
        lines.append("")
        lines.append("**For your year-end CPA** (running; updated nightly)")
        floor_note = _floor_note(cpa)
        if floor_note:
            lines.append(floor_note)
        candidates = cpa.get("form_1099_candidates", [])
        needs_action = [c for c in candidates if w9_gap(c)]
        if candidates and not appendix_changed:
            due = sum(1 for c in candidates if c.get("form_1099_due", True))
            gaps = _w9_clause(candidates, needs_action)
            lines.append(
                f"- 1099 appendix unchanged: {len(candidates)} taxpayer(s) over the floor, "
                f"{due} due a 1099-NEC{gaps} (the full list reprints when a taxpayer, "
                f"W-9, or classification changes)"
            )
            for c in needs_action:
                lines.append(_candidate_line(c))
        else:
            if candidates:
                for c in candidates:
                    lines.append(_candidate_line(c))
            else:
                lines.append("- no 1099-shaped vendor has crossed the floor yet")
            groups = cpa.get("tax_entity_paid_totals", {})
            for entity, cents in sorted(groups.items()):
                lines.append(f"- tax entity {entity}: {_dollars(cents)} paid combined")
    return "\n".join(lines)
