"""Authority in the daily loop (#435; docs/tenant-kit-design.md, section 3).

A tenant with ``authority.toml`` decides cards and lanes through
``core.authority.evaluate``; a tenant without one never reaches this module's
decisions (``load_policy`` returns None and every caller keeps today's path).
"""

from __future__ import annotations

import hashlib
import json
import re
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
    card_month,
    card_party,
    card_request,
    evaluate,
    parse_policy,
)
from ..authority.routing import CardRoute, Delegation, decide, route_for, waiting_on
from ..policy import RULES_DIR, PolicyError, Rulebook, load_rulebook
from .config import load_tenant, tenant_dir
from .kit import KIT_FILES, load_part
from .registry import load_card_authority, load_human_only

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
    rules: Rulebook | None = None
    """The ministry's policy rules (core/policy), every one drafted, in force
    or not: ``policy/propose`` cards them, and only an approved card puts
    one to work (core/agents/policy)."""
    rules_error: str = ""
    """Why the rules did not load, said by the jobs that read them; the
    policy itself still holds, so one bad file never stops every lane."""


def load_policy(slug: str, *, tenants_root: Path | None = None) -> TenantAuthority | None:
    """The tenant's policy, or None when it has no authority.toml."""
    directory = tenant_dir(slug, tenants_root=tenants_root)
    data = load_part(directory, "authority")
    if data is None:
        return None
    raw = (directory / KIT_FILES["authority"]).read_bytes()
    policy = parse_policy(data)
    try:
        rules, error = _rules(slug, tenants_root, policy), ""
    except PolicyError as exc:
        rules, error = None, str(exc)
    return TenantAuthority(policy, hashlib.sha256(raw).hexdigest(), rules, error)


def _rules(slug: str, tenants_root: Path | None, policy: Policy | None) -> Rulebook | None:
    directory = tenant_dir(slug, tenants_root=tenants_root)
    if not (directory / RULES_DIR).is_dir():
        return None  # the common case reads nothing more
    kinds = list(
        load_tenant(slug, tenants_root=tenants_root, check_evals=False).expenses.category_accounts
    )
    return load_rulebook(
        directory,
        roles=set(policy.roles) if policy else None,
        people=set(policy.people) if policy else None,
        kinds=kinds or None,
    )


def load_rules(slug: str, *, tenants_root: Path | None = None) -> Rulebook | None:
    """The ministry's policy rules (core/policy), checked against its own
    roles, people and categories; None when it has no ``rules/`` folder.
    Raises ``PolicyError`` naming what a file says that cannot hold."""
    authority = load_policy(slug, tenants_root=tenants_root)
    if authority is None:
        return _rules(slug, tenants_root, None)
    if authority.rules_error:
        raise PolicyError(authority.rules_error)
    return authority.rules


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


def _word_refusal(
    policy: Policy, rule: CardRule, principal: str, row: dict, said: str, others: list[dict]
) -> str:
    """Why the person's word to their own AI, with no Face ID, does not
    decide this card, or "" (the owner's decision of 2026-10-11: under the
    tenant's ``yes_by_word_up_to`` line, the word is the yes, when the words
    name the card, 2026-10-12)."""
    if rule.resource not in policy.door:
        return (
            f"{rule.resource} is decided at a terminal: authority.toml's [presence] door "
            "does not open it"
        )
    line = policy.safeguards.yes_by_word_up_to
    if line <= 0:
        return "every yes through the door takes the person's own Face ID"
    if row["params"].get("human_only") == "true" or row["action_type"] in load_human_only(
        str(row["agent"])
    ):
        return "a human-only card takes the person's own Face ID"
    if not rule.money:
        return "a card that commits no money takes the person's own Face ID"
    if row["params"].get("policy_face_id") == "true":
        return "the ministry's rules ask for the person's own Face ID on this one"
    amount = card_amount(rule, row["params"])
    if amount is None:
        return "the card does not say how much, so it takes the person's own Face ID"
    if amount > line:
        return f"{amount} is over the {line} line, so it takes the person's own Face ID"
    if not _names_the_card(policy, rule, principal, row["params"], amount, said):
        return (
            "the person's words name neither whose card it is nor how much, so it takes "
            "the person's own Face ID"
        )
    if _fits_another(rule, row["params"], amount, said, others):
        return (
            "the person's words fit more than one waiting card; without its month or "
            "amount, it takes the person's own Face ID"
        )
    return ""


_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _says(words: list[str], name: list[str]) -> bool:
    """``name`` appears in ``words``, in order; "Ruth's" and "ruths" name Ruth."""
    n = len(name)
    return any(
        words[i : i + n - 1] == name[:-1] and words[i + n - 1] in (name[-1], name[-1] + "s")
        for i in range(len(words) - n + 1)
    )


_MINE = frozenset({"my", "mine"})
_LEGAL = frozenset(
    {"inc", "llc", "ltd", "lda", "co", "corp", "company", "corporation", "pllc", "llp"}
)


def _names_the_card(
    policy: Policy, rule: CardRule, principal: str, params: dict, amount: Decimal, said: str
) -> bool:
    """The person's own words name this card: its amount, its person's full
    name, or their first name when no one else in authority.toml shares it
    (the owner's decision of 2026-10-12, after a small model approved a card
    by word on "approve everything waiting for Africa", none of it hers).
    "My" or "mine" from the person the card is about names it as their own
    name would (a solo missionary's "approve my report"), and a bill is
    named by whom it pays without its "Co." or "Inc." (same day)."""
    if any(Decimal(m.replace(",", "")) == amount for m in _AMOUNT.findall(said)):
        return True
    words = _words(said)
    who, payee = card_party(rule, params)
    name = _words(who or payee)
    if not name or not words:
        return False
    if who and _MINE & set(words) and card_request(rule, principal, params).submitter == principal:
        return True
    if _says(words, name):
        return True
    if not who:
        while len(name) > 1 and name[-1] in _LEGAL:
            name = name[:-1]
        return _says(words, name)
    first, slug = name[0], "-".join(name)
    shared = any(p != slug and p.split("-")[0] == first for p in policy.people)
    return not shared and _says(words, [first])


def _fits_another(
    rule: CardRule, params: dict, amount: Decimal, said: str, others: list[dict]
) -> bool:
    """Words that name this card by whose it is also fit another waiting card
    about the same person or payee, unless they say this card's amount or a
    month only it is for (variance 2026-10-12: told "approve my September
    report", a small model approved August's by word; "my" named both)."""
    if any(Decimal(m.replace(",", "")) == amount for m in _AMOUNT.findall(said)):
        return False
    party = card_party(rule, params)
    twins = [o for o in others if card_party(_rule_of(o), o["params"]) == party]
    if not twins:
        return False
    month = card_month(params).split(" ")[0].casefold()
    if not month or month not in _words(said):
        return True
    return any(card_month(o["params"]).split(" ")[0].casefold() == month for o in twins)


def _rule_of(row: dict) -> CardRule:
    return load_card_authority(str(row["agent"])).get(str(row["action_type"]), UNDESCRIBED)


def route_of(policy: Policy, row: dict, now: str) -> tuple[CardRule, CardRoute]:
    """The card's rule and its route: the one kept on the card, or a new
    one computed from authority.toml, opened ``now``."""
    rule = _rule_of(row)
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
    by_word: bool = False,
    said: str = "",
    others: list[dict] | None = None,
) -> CardDecision:
    """``principal`` decides this card on its route (#436). A person decides
    when present: at a terminal, or witnessed by the box's door (their own
    Face ID, on a resource the tenant opened with ``[presence] door``, or the
    Face ID of the person ``[vouch]`` names for them, who is not the card's
    subject, has not voted on it, and votes on it no more); a headless
    caller decides only as an agent. A rejection by anyone who may
    decide the current step ends it; an approval is a vote, and the card
    resolves when the route's last step has its approvers. ``by_word``: the
    person said it to their own AI through the door, with no Face ID; it
    decides a money card at or under ``[safeguards] yes_by_word_up_to``
    when ``said``, their own words, name the card, and say which when they
    would fit any of ``others`` (the cards still waiting) as well."""
    who = policy.principal(principal)
    if who is None:
        return CardDecision(f"{principal!r} is not a person or agent in authority.toml")
    rule, route = route_of(policy, row, now)
    by_door = who.kind == "person" and not at_terminal and witness is not None
    word = who.kind == "person" and not at_terminal and witness is None and by_word
    if who.kind == "person" and not at_terminal:
        refused = _presence_refusal(
            policy, rule, principal, row, decision, witness, word, said, others or []
        )
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
    by = witness if by_door else None
    stamp.update(_presence_stamp(policy, rule, by, voucher, word, said))
    return CardDecision(final=True, stamp=stamp)


def _presence_refusal(
    policy: Policy,
    rule: CardRule,
    principal: str,
    row: dict,
    decision: str,
    witness: Witnessed | None,
    word: bool,
    said: str,
    others: list[dict],
) -> str:
    """Why a person away from a terminal is not present for this card, or ""."""
    if word:
        return _word_refusal(policy, rule, principal, row, said, others)
    if witness is None:
        return (
            f"{principal} is a person: a person decides at a terminal or by their own "
            "Face ID through the box's door; a headless caller decides only as an agent"
        )
    return _door_refusal(policy, rule, principal, row, decision, witness)


def _presence_stamp(
    policy: Policy,
    rule: CardRule,
    witness: Witnessed | None,
    voucher: str,
    word: bool,
    said: str,
) -> dict[str, str]:
    """How the person was present, kept on the decided card: by word, the
    words themselves, for the auditor."""
    if word:
        return {
            "decided_via": "door-word",
            "yes_by_word_up_to": str(policy.safeguards.yes_by_word_up_to),
            "said": said,
        }
    if witness is None:
        return {}
    stamp = {"decided_via": "door-vouched" if voucher else "door"}
    if voucher:
        stamp["vouched_by"] = voucher
    stamp["witness_at"] = datetime.fromtimestamp(witness.at, UTC).isoformat()
    stamp["witness_resource"] = rule.resource  # what the auditor holds to [presence] door
    stamp["witness"] = json.dumps({**witness.evidence, "summary": witness.summary})
    return stamp


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
