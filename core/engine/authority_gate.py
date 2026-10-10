"""Authority in the daily loop (#435; docs/tenant-kit-design.md, section 3).

A tenant with ``authority.toml`` decides cards and lanes through
``core.authority.evaluate``; a tenant without one never reaches this module's
decisions (``load_policy`` returns None and every caller keeps today's path).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..authority import (
    UNDESCRIBED,
    CardRule,
    Decision,
    Policy,
    Request,
    card_amount,
    card_request,
    evaluate,
    parse_policy,
)
from ..authority.routing import CardRoute, Delegation, decide, route_for, waiting_on
from .config import tenant_dir
from .kit import KIT_FILES, load_part
from .registry import load_card_authority

DECISION_PARAMS = frozenset(
    {
        "decided_by",
        "authority_reason",
        "on_behalf_of",
        "approvers",
        "route",
        "route_step",
        "route_votes",
        "route_opened",
        "handoff_to",
        "on_it_until",
        "on_it_set",
    }
)
"""Card params only the queue writes under authority; ``--param`` naming
one is refused, like the human-only gate fields."""


@dataclass(frozen=True)
class TenantAuthority:
    policy: Policy
    sha256: str  # of the file as read, so a run key moves when it changes


def load_policy(slug: str, *, tenants_root: Path | None = None) -> TenantAuthority | None:
    """The tenant's policy, or None when it has no authority.toml."""
    directory = tenant_dir(slug, tenants_root=tenants_root)
    data = load_part(directory, "authority")
    if data is None:
        return None
    raw = (directory / KIT_FILES["authority"]).read_bytes()
    return TenantAuthority(parse_policy(data), hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class CardDecision:
    """One decision on a card under authority: refused (``refusal``), a
    vote on a route with more to go (``final`` False: ``stamp`` is the route
    state to keep on the card), or the decision that resolves it."""

    refusal: str = ""
    final: bool = False
    stamp: dict[str, str] | None = None
    waiting_on: tuple[str, ...] = ()
    step: str = ""


@dataclass(frozen=True)
class Witnessed:
    """A person's own yes or no on one card, witnessed by the box's door:
    the doorkeeper checked their Face ID or fingerprint against the passkey
    on their phone (doorkeeper ``/witness/check``). ``evidence`` is the
    signed answer, kept on the card so it can be checked again."""

    person: str
    card: str
    verb: str  # approve | reject
    summary: str
    at: float
    evidence: dict = field(default_factory=dict)


VERB_OF = {"approved": "approve", "rejected": "reject"}


def _door_refusal(
    policy: Policy, rule: CardRule, principal: str, row: dict, decision: str, witness: Witnessed
) -> str:
    """Why this witness does not make the person present, or ""."""
    if rule.resource not in policy.door:
        return (
            f"{rule.resource} is decided at a terminal: authority.toml's [presence] door "
            "does not open it to a witnessed yes"
        )
    if witness.person != principal and policy.vouch.get(principal) != witness.person:
        return f"the witness is {witness.person}'s, not {principal}'s"
    if witness.card != str(row["id"]):
        return f"the witness is for card {witness.card}, not #{row['id']}"
    if witness.verb != VERB_OF.get(decision):
        return f"the witness says {witness.verb}, not {VERB_OF.get(decision, decision)}"
    return ""


def route_of(policy: Policy, row: dict, now: str) -> tuple[CardRule, CardRoute]:
    """The card's rule and its route: the one kept on the card, or a new
    one computed from authority.toml, opened ``now``."""
    rule = load_card_authority(str(row["agent"])).get(str(row["action_type"]), UNDESCRIBED)
    route = CardRoute.from_params(row["params"])
    if route is None:
        steps = route_for(policy, rule, card_amount(rule, row["params"]))
        route = CardRoute(steps=steps, opened=str(row.get("created_at") or now))
    return rule, route


def decide_card(
    policy: Policy,
    principal: str,
    row: dict,
    *,
    decision: str,
    at_terminal: bool,
    delegations: list[Delegation],
    today: date,
    now: str,
    witness: Witnessed | None = None,
) -> CardDecision:
    """``principal`` decides this card on its route (#436). A person decides
    when present: at a terminal, or witnessed by the box's door (their own
    Face ID, on a resource the tenant opened with ``[presence] door``, or the
    Face ID of the person ``[vouch]`` names for them, who is not the card's
    subject, has not voted on it, and votes on it no more); a headless
    caller decides only as an agent. A rejection by anyone who may
    decide the current step ends it; an approval is a vote, and the card
    resolves when the route's last step has its approvers."""
    who = policy.principal(principal)
    if who is None:
        return CardDecision(f"{principal!r} is not a person or agent in authority.toml")
    rule, route = route_of(policy, row, now)
    by_door = who.kind == "person" and not at_terminal and witness is not None
    if who.kind == "person" and not at_terminal:
        if witness is None:
            return CardDecision(
                f"{principal} is a person: a person decides at a terminal or by their own "
                "Face ID through the box's door; a headless caller decides only as an agent"
            )
        refused = _door_refusal(policy, rule, principal, row, decision, witness)
        if refused:
            return CardDecision(refused)
    request = card_request(rule, principal, row["params"])
    vouchers = [v for v in str(row["params"].get("vouchers", "")).split(",") if v]
    if principal in vouchers:
        return CardDecision(f"{principal} vouched on this card, so does not also decide it")
    voucher = witness.person if by_door and witness and witness.person != principal else ""
    if voucher:
        if voucher == request.submitter:
            return CardDecision(f"{voucher} cannot vouch on their own card")
        if any(voucher in (actor, auth) for _s, actor, auth in route.votes):
            return CardDecision(f"{voucher} already voted on this card, so cannot also vouch")
        vouchers.append(voucher)
    out = decide(
        policy,
        rule,
        request,
        route,
        decision=decision,
        delegations=delegations,
        today=today,
        now=now,
    )
    if out.refusal or out.route is None or out.verdict is None:
        return CardDecision(out.refusal or "refused")
    state = out.route.to_params()
    if voucher:
        state["vouchers"] = ",".join(vouchers)  # the voucher votes on this card no more
    total = len(out.route.steps)
    if not out.final:
        return CardDecision(
            final=False,
            stamp=state,
            waiting_on=tuple(waiting_on(policy, rule, out.route, delegations, today)),
            step=f"step {out.route.index + 1} of {total}",
        )
    stamp = {**state, "decided_by": principal, "authority_reason": out.verdict.reason}
    if out.authority != principal:
        stamp["on_behalf_of"] = out.authority
    if decision == "approved":
        stamp["approvers"] = ", ".join(dict.fromkeys(a for _s, a, _auth in out.route.votes))
    if out.verdict.review_after:
        stamp["review_after"] = "true"
    if out.verdict.report:
        stamp["self_approved"] = "true"
    if by_door and witness is not None:
        stamp["decided_via"] = "door-vouched" if voucher else "door"
        if voucher:
            stamp["vouched_by"] = voucher
        stamp["witness_at"] = datetime.fromtimestamp(witness.at, UTC).isoformat()
        stamp["witness_resource"] = rule.resource  # what the auditor holds to [presence] door
        stamp["witness"] = json.dumps({**witness.evidence, "summary": witness.summary})
    return CardDecision(final=True, stamp=stamp)


def waiting(policy: Policy, ledger: Any, tenant: str, *, today: date, now: str) -> list[dict]:
    """Every pending card on its route: what it is, its step, who it waits
    on and by when (the brief's line for the process owner, and
    ``engine queue waiting``)."""
    from ..authority.routing import due_date

    delegations = load_delegations(ledger, tenant)
    out = []
    for row in ledger.list_approvals(tenant, status="pending"):
        rule, route = route_of(policy, row, now)
        out.append(
            {
                "card": row["id"],
                "action_type": row["action_type"],
                "step": f"step {route.index + 1} of {len(route.steps)}",
                "waiting_on": waiting_on(policy, rule, route, delegations, today),
                "due": due_date(route, row["params"], policy.routing).isoformat(),
                "route": route,
                "params": row["params"],
            }
        )
    return out


def load_delegations(ledger: Any, tenant: str) -> list[Delegation]:
    """Every delegation given and not revoked (``route.delegated`` /
    ``route.revoked`` events from the routing lane); the caller checks
    each one's dates."""
    rows = ledger.conn.execute(
        "SELECT event_type, payload_json FROM events WHERE tenant = ? AND event_type IN "
        "('route.delegated', 'route.revoked') ORDER BY id",
        (tenant,),
    ).fetchall()
    given: dict[str, Delegation] = {}
    for r in rows:
        p = json.loads(r["payload_json"] or "{}")
        if r["event_type"] == "route.revoked":
            given.pop(str(p.get("delegation", "")), None)
            continue
        start, until = date.fromisoformat(p["start"]), date.fromisoformat(p["until"])
        given[str(p["delegation"])] = Delegation(p["by"], p["to"], p["role"], start, until)
    return list(given.values())


def lane_decision(
    policy: Policy, lane: str, action: str, resource: str
) -> tuple[str, Decision] | None:
    """Whether the agent a lane runs as may take ``action`` on ``resource``
    through a card with no one asked: (agent, decision) when allowed, None
    when no agent claims the lane or its grants do not reach."""
    agent = policy.agent_for_lane(lane)
    if agent is None:
        return None
    verdict = evaluate(
        policy, Request(agent.name, action, resource, amount=Decimal("0"), via_card=True)
    )
    return (agent.name, verdict) if verdict.allowed else None


__all__ = [
    "DECISION_PARAMS",
    "TenantAuthority",
    "Witnessed",
    "CardDecision",
    "decide_card",
    "lane_decision",
    "load_delegations",
    "load_policy",
    "route_of",
    "waiting",
]
