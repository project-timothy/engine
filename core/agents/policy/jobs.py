"""Policy agent jobs: propose (chunk 5, board item t-policy-rules).

A ministry's handbook becomes rules under ``<tenant>/rules/`` (core/policy).
Whoever drafts them, a model or a person, nothing drafted is in force:
``propose`` raises one card per rule, keyed to the rule's exact words and
amounts (its fingerprint), and a rule is in force only while an approved
card carries that fingerprint. New words are a new card, which retires the
old one still waiting; an approval given to the old words never covers the
new ones.

Deciding the card is confirming an authority change (``confirm:authority``),
so the floor holds: no agent confirms one, and a card that commits no money
always takes the person's own Face ID through the door. During the alpha
the owner approves every rule (2026-10-14); from beta, the ministry's own
structure does, by the same grant.
"""

from __future__ import annotations

import json
import sqlite3

from ...authority import CardRule
from ...engine.contracts import ApprovalSpec, JobContext, JobHandler, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ...ledger import Ledger
from ...policy import Rule, Rulebook

RULE_CARD = "policy.rule"


def _rule_cards(ledger: Ledger, tenant: str) -> list[dict]:
    rows = ledger.conn.execute(
        "SELECT id, status, idempotency_key, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? ORDER BY id",
        (tenant, RULE_CARD),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "status": r["status"],
            "key": r["idempotency_key"],
            "params": json.loads(r["params_json"] or "{}"),
        }
        for r in rows
    ]


def approved_rule_shas(conn: sqlite3.Connection, tenant: str) -> set[str]:
    """The fingerprints a person said yes to (an approved ``policy.rule`` card)."""
    rows = conn.execute(
        "SELECT params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'approved'",
        (tenant, RULE_CARD),
    ).fetchall()
    return {str(json.loads(r[0] or "{}").get("sha", "")) for r in rows}


def rules_in_force(ledger: Ledger, tenant: str, book: Rulebook | None) -> list[Rule]:
    """The rules a person said yes to, word for word, highest layer first."""
    if book is None:
        return []
    approved = approved_rule_shas(ledger.conn, tenant)
    return [r for r in book.rules if r.sha in approved]


def _book(ctx: JobContext) -> tuple[Rulebook | None, str]:
    authority = ctx.authority
    if authority is None:
        return None, ""
    return authority.rules, authority.rules_error


def _key(ctx: JobContext) -> str:
    key = RunKey(ctx, "policy-propose")
    book, error = _book(ctx)
    key.value("rules", sorted(r.sha for r in book.rules) if book else [])
    key.value("rules_error", error)
    key.value("cards", [(c["id"], c["status"]) for c in _rule_cards(ctx.ledger, ctx.tenant_slug)])
    return key.digest()


def _card_key(rule: Rule) -> str:
    return f"rule:{rule.layer}:{rule.id}:{rule.sha[:16]}"


def _propose_run(ctx: JobContext) -> JobOutput:
    book, error = _book(ctx)
    if error:
        return JobOutput(
            status="error",
            summary="policy propose: the rules did not load; nothing was carded",
            anomalies=[Anomaly(code="policy.rules_unreadable", detail=error)],
        )
    if book is None or not book.rules:
        return JobOutput(status="ok", summary="policy propose: no rules for this ministry")
    cards = _rule_cards(ctx.ledger, ctx.tenant_slug)
    carded = {c["params"].get("sha") for c in cards}
    prefix = f"apr:{ctx.tenant_slug}:policy:{RULE_CARD}:"
    approvals = []
    for rule in book.rules:
        if rule.sha in carded:
            continue
        older = [
            c["key"][len(prefix) :]
            for c in cards
            if c["status"] == "pending"
            and c["key"].startswith(prefix)
            and c["params"].get("rule") == rule.id
            and c["params"].get("layer") == rule.layer
        ]
        approvals.append(
            ApprovalSpec(
                key=_card_key(rule),
                action_type=RULE_CARD,
                params={
                    # How the door names the card (decide.card_words).
                    "what": f'the rule "{rule.says}" from {rule.cite()}',
                    "rule": rule.id,
                    "layer": rule.layer,
                    "says": rule.says,
                    "from": rule.cite(),
                    "quote": rule.source.quote,
                    "sha": rule.sha,
                },
                reason="a drafted rule is in force only after a person's yes to its exact words",
                supersedes_keys=older,
            )
        )
    waiting = sum(1 for c in cards if c["status"] == "pending") + len(approvals)
    in_force = len(rules_in_force(ctx.ledger, ctx.tenant_slug, book))
    summary = (
        f"policy propose: {len(book.rules)} rule(s), {in_force} in force; "
        f"{len(approvals)} new card(s)"
    )
    if ctx.shadow:
        return JobOutput(status="ok", summary=summary + " (shadow: none parked)")
    return JobOutput(
        status="needs_approval" if approvals else "ok",
        summary=summary + (f", {waiting} waiting" if waiting else ""),
        approvals=approvals,
    )


JOBS: dict[str, JobHandler] = {"propose": JobHandler(key=_key, run=_propose_run)}


# Deciding a rule is confirming an authority change: the floor keeps agents
# out, and a card with no money takes the person's own Face ID at the door.
CARD_AUTHORITY: dict[str, CardRule] = {RULE_CARD: CardRule("confirm", "authority", money=False)}
