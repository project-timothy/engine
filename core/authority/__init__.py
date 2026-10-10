"""The authority model (docs/tenant-kit-design.md, section 3; issue #432).

One question: may this principal take this action on this resource, in this
context? The request is shaped the way Cedar shapes one (principal, action,
resource, context), so a later move to Cedar is a translation of the policy
file, not a redesign.

Two layers, in this order:

1. **The floor**, in code. No ``authority.toml`` switches it off: money and
   external sends happen only through a card (invariant 7); no agent releases
   money while ``[money].out`` is ``"human"`` (the only value the engine
   accepts); no agent confirms an authority change; an agent that reads
   outside documents never proposes one; and every authority decision carries
   ``record=True`` so the caller writes it down.
2. **Grants and safeguards**, in data. Default deny, a forbid always wins,
   ``own`` scopes stop sideways movement, limits cap amounts, and nobody
   grants what they do not hold. The safeguards (self-approval, a second
   approver, a distinct payer, who confirms an authority change, the monthly
   review-after) are set per shape and changeable by the tenant.

#435 wires it into the queue: ``CardRule`` says what deciding a card is, and
``card_request`` builds the request a decision is evaluated as.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

from .errors import AuthorityError
from .routes import Routing, parse_routing

ACTIONS = ("view", "submit", "approve", "release", "send", "review", "propose", "confirm")
"""What a principal can do. ``release`` moves money and ``send`` leaves the
building, so both happen only through a card; ``propose`` and ``confirm``
are the two halves of an authority change."""

RESOURCES = (
    "ap.invoice",
    "expense.report",
    "purchase.order",
    "payment",
    "message",
    "authority",
    "vendor",
    "books",
    "calendar",
    "card",
)
"""What it is done to. ``authority`` is this policy itself; ``vendor`` is a
new vendor or a vendor's tax facts (the new-vendor door); ``books`` is a
write to the accounting system or the month's lock; ``calendar`` is a write
to someone's calendar; ``card`` is a card type no agent has described, which
only ``approve:*`` or ``approve:card`` decides."""

SCOPES = ("any", "own")
"""``any``: every project, fund or unit. ``own``: only the ones the
principal is assigned to."""

SELF_APPROVAL = ("allowed", "below_limit", "never")
REVIEW_AFTER = ("none", "monthly")
DISTINCT_PAYER = ("off", "above_limit", "always")
AUTHORITY_CONFIRM = ("self", "second_person", "group")
REPORT_SELF_APPROVALS = ("never", "above_limit", "always")

MONEY_OUT = ("human",)

_PERMISSION = re.compile(
    r"^(?P<action>\*|[a-z]+):(?P<resource>\*|[a-z][a-z.]*)"
    r"(?:@(?P<scope>[a-z]+))?(?:<=(?P<limit>\d+(?:\.\d{1,2})?))?$"
)


@dataclass(frozen=True)
class Permission:
    action: str
    resource: str
    scope: str = "any"
    limit: Decimal | None = None
    text: str = ""

    def matches(self, action: str, resource: str) -> bool:
        return self.action in ("*", action) and self.resource in ("*", resource)


def parse_permission(text: str) -> Permission:
    """``action:resource[@scope][<=limit]``, for example
    ``approve:expense.report@own<=500``."""
    match = _PERMISSION.match(text.strip())
    if not match:
        raise AuthorityError(f"{text!r} is not action:resource[@scope][<=limit]")
    action, resource = match["action"], match["resource"]
    scope = match["scope"] or "any"
    if action != "*" and action not in ACTIONS:
        raise AuthorityError(f"{text!r}: unknown action {action!r} ({', '.join(ACTIONS)})")
    if resource != "*" and resource not in RESOURCES:
        raise AuthorityError(f"{text!r}: unknown resource {resource!r} ({', '.join(RESOURCES)})")
    if scope not in SCOPES:
        raise AuthorityError(f"{text!r}: scope must be one of {', '.join(SCOPES)}")
    limit = Decimal(match["limit"]) if match["limit"] else None
    return Permission(action, resource, scope, limit, text.strip())


@dataclass(frozen=True)
class Role:
    grants: tuple[Permission, ...] = ()
    forbids: tuple[Permission, ...] = ()


@dataclass(frozen=True)
class Principal:
    name: str
    kind: str  # person | agent
    roles: tuple[str, ...]
    scopes: tuple[str, ...] = ()
    reads_outside: bool = False
    lanes: tuple[str, ...] = ()
    backup: str = ""  # a person's named backup, for "covering for" (#436)


@dataclass(frozen=True)
class Safeguards:
    """Defaults are the strictest setting, so a file that leaves the table
    out asks for more control, never less."""

    self_approval: str = "never"
    self_approval_limit: Decimal = Decimal("0")
    review_after: str = "none"
    distinct_payer: str = "always"
    distinct_payer_above: Decimal = Decimal("0")
    second_approver_above: Decimal = Decimal("0")
    authority_confirm: str = "second_person"
    report_self_approvals: str = "always"


@dataclass(frozen=True)
class Policy:
    money_out: str
    safeguards: Safeguards
    roles: dict[str, Role] = field(default_factory=dict)
    people: dict[str, Principal] = field(default_factory=dict)
    agents: dict[str, Principal] = field(default_factory=dict)
    routing: Routing = field(default_factory=Routing)
    door: frozenset[str] = frozenset()
    """Resources a person may decide through the box's door, witnessed by
    their own Face ID (``[presence] door``); empty means a terminal only."""
    vouch: dict[str, str] = field(default_factory=dict)
    """Person -> the person who may confirm their yes with their own Face ID
    when the first one's phone cannot (``[vouch]``)."""

    def holders(self, role: str) -> list[str]:
        """The people holding ``role``, in file order."""
        return [p.name for p in self.people.values() if role in p.roles]

    def principal(self, name: str) -> Principal | None:
        return self.people.get(name) or self.agents.get(name)

    def agent_for_lane(self, lane: str) -> Principal | None:
        """The agent principal an engine lane (``core/agents/<lane>``) runs
        as; None when no agent claims it, so the lane holds no grants."""
        return next((a for a in self.agents.values() if lane in a.lanes), None)

    def grants_of(self, who: Principal) -> list[Permission]:
        return [g for r in who.roles for g in self.roles[r].grants]

    def forbids_of(self, who: Principal) -> list[Permission]:
        return [f for r in who.roles for f in self.roles[r].forbids]

    def holds_any_scope(self, name: str, action: str, resource: str) -> bool:
        """Whether ``name`` holds this action on every scope (the only way a
        request outside their own units may be allowed)."""
        who = self.principal(name)
        if who is None:
            return False
        return any(g.scope == "any" and g.matches(action, resource) for g in self.grants_of(who))

    def with_people(self, people: dict[str, tuple[list[str], list[str]]]) -> Policy:
        """A copy with ``people`` (name -> (roles, scopes)) in place of its
        own; for onboarding previews and the enumeration tests."""
        built = {
            name: Principal(name, "person", tuple(roles), tuple(scopes))
            for name, (roles, scopes) in people.items()
        }
        for name, principal in built.items():
            _check_roles(name, principal.roles, self.roles)
        return replace(self, people=built)


@dataclass(frozen=True)
class Request:
    principal: str
    action: str
    resource: str
    scope: str = ""
    amount: Decimal | None = None
    submitter: str = ""
    payee: str = ""
    approver: str = ""
    proposer: str = ""
    via_card: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    needs_second: bool = False
    review_after: bool = False
    report: bool = False
    record: bool = False


# ---- parsing --------------------------------------------------------------------


def _choice(table: dict, key: str, options: tuple[str, ...], default: str) -> str:
    value = table.get(key, default)
    if value not in options:
        raise AuthorityError(f"[safeguards] {key} = {value!r}: use one of {', '.join(options)}")
    return str(value)


def _amount(table: dict, key: str) -> Decimal:
    try:
        value = Decimal(str(table.get(key, 0)))
    except InvalidOperation as exc:
        raise AuthorityError(f"[safeguards] {key} must be an amount") from exc
    if value < 0:
        raise AuthorityError(f"[safeguards] {key} must not be negative")
    return value


def _check_roles(name: str, roles: tuple[str, ...], known: dict[str, Role]) -> None:
    for role in roles:
        if role not in known:
            raise AuthorityError(f"{name} names role {role!r}, which [roles] does not define")


def _permissions(name: str, values: object, *, forbids: bool) -> tuple[Permission, ...]:
    if not isinstance(values, list):
        raise AuthorityError(f"[roles.{name}] grants and forbids are lists")
    perms = tuple(parse_permission(str(v)) for v in values)
    if forbids and any(p.limit is not None for p in perms):
        raise AuthorityError(f"[roles.{name}] a forbid takes no limit: it forbids outright")
    return perms


def parse_policy(data: dict) -> Policy:
    """A parsed ``authority.toml``. Raises ``AuthorityError`` naming the
    first thing it cannot hold."""
    money = data.get("money")
    if not isinstance(money, dict) or "out" not in money:
        raise AuthorityError("authority.toml has no [money] out = ...; the engine needs it said")
    if money["out"] not in MONEY_OUT:
        raise AuthorityError(
            f'[money] out = {money["out"]!r}: the engine accepts only "human"; '
            "anything else waits for the payment-rails design"
        )
    sg = data.get("safeguards", {})
    safeguards = Safeguards(
        self_approval=_choice(sg, "self_approval", SELF_APPROVAL, "never"),
        self_approval_limit=_amount(sg, "self_approval_limit"),
        review_after=_choice(sg, "review_after", REVIEW_AFTER, "none"),
        distinct_payer=_choice(sg, "distinct_payer", DISTINCT_PAYER, "always"),
        distinct_payer_above=_amount(sg, "distinct_payer_above"),
        second_approver_above=_amount(sg, "second_approver_above"),
        authority_confirm=_choice(sg, "authority_confirm", AUTHORITY_CONFIRM, "second_person"),
        report_self_approvals=_choice(sg, "report_self_approvals", REPORT_SELF_APPROVALS, "always"),
    )
    roles = {
        name: Role(
            grants=_permissions(name, table.get("grants", []), forbids=False),
            forbids=_permissions(name, table.get("forbids", []), forbids=True),
        )
        for name, table in data.get("roles", {}).items()
    }
    people: dict[str, Principal] = {}
    for name, table in data.get("people", {}).items():
        person = Principal(
            name,
            "person",
            tuple(table.get("roles", [])),
            tuple(table.get("scopes", [])),
            backup=str(table.get("backup", "")),
        )
        _check_roles(name, person.roles, roles)
        people[name] = person
    agents: dict[str, Principal] = {}
    for name, table in data.get("agents", {}).items():
        if not isinstance(table.get("reads_outside"), bool):
            raise AuthorityError(
                f"[agents.{name}] must say reads_outside = true or false: an agent that "
                "reads mail, invoices or statements never proposes an authority change"
            )
        lanes = table.get("lanes", [])
        if not isinstance(lanes, list) or not all(isinstance(x, str) for x in lanes):
            raise AuthorityError(f"[agents.{name}] lanes is a list of lane names")
        agent = Principal(
            name,
            "agent",
            tuple(table.get("roles", [])),
            tuple(table.get("scopes", [])),
            table["reads_outside"],
            tuple(lanes),
        )
        _check_roles(name, agent.roles, roles)
        for lane in agent.lanes:
            other = next((a.name for a in agents.values() if lane in a.lanes), None)
            if other:
                raise AuthorityError(f"lane {lane!r} is claimed by both {other} and {name}")
        agents[name] = agent
    overlap = set(people) & set(agents)
    if overlap:
        raise AuthorityError(f"{', '.join(sorted(overlap))} is both a person and an agent")
    for person in people.values():
        if person.backup and (person.backup not in people or person.backup == person.name):
            raise AuthorityError(f"[people.{person.name}] backup names another person")
    routing = parse_routing(
        data.get("routing", {}), roles=set(roles), people=set(people), resources=RESOURCES
    )
    door = _door(data.get("presence", {}))
    vouch = _vouch(data.get("vouch", {}), people)
    return Policy(str(money["out"]), safeguards, roles, people, agents, routing, door, vouch)


def _vouch(table: object, people: dict) -> dict[str, str]:
    """``[vouch] don-pruitt = "carol-jennings"``: when Don's phone cannot do
    Face ID, Carol may confirm his yes with hers. Both are people the
    ministry named, and nobody vouches for themselves."""
    if not isinstance(table, dict):
        raise AuthorityError("[vouch] maps a person to the person who may confirm their yes")
    for person, voucher in table.items():
        if person not in people or not isinstance(voucher, str) or voucher not in people:
            raise AuthorityError(f"[vouch] {person} = {voucher!r}: both must be people in [people]")
        if voucher == person:
            raise AuthorityError(f"[vouch] {person} cannot vouch for themselves")
    return dict(table)


def _door(table: object) -> frozenset[str]:
    """``[presence] door = ["expense.report", ...]``: the resources a person
    may decide away from a terminal, when the box's door witnessed their own
    yes (Face ID on their phone). Left out, a person decides at a terminal."""
    if not isinstance(table, dict) or set(table) - {"door"}:
        raise AuthorityError("[presence] takes one setting: door = [resources]")
    door = table.get("door", [])
    if not isinstance(door, list) or not all(isinstance(r, str) for r in door):
        raise AuthorityError("[presence] door is a list of resources")
    unknown = [r for r in door if r not in RESOURCES]
    if unknown:
        raise AuthorityError(
            f"[presence] door names {', '.join(unknown)}; resources are {', '.join(RESOURCES)}"
        )
    return frozenset(door)


# ---- evaluation -----------------------------------------------------------------


def _in_scope(perm: Permission, who: Principal, scope: str) -> bool:
    return perm.scope == "any" or (bool(scope) and scope in who.scopes)


def _within_limit(perm: Permission, amount: Decimal | None) -> bool:
    return perm.limit is None or (amount is not None and amount <= perm.limit)


def _floor(policy: Policy, who: Principal, req: Request) -> str:
    """Why the floor refuses this request, or "" when it does not. Runs
    before any grant is read, so no authority.toml can switch it off."""
    agent = who.kind == "agent"
    if req.action in ("release", "send") and not req.via_card:
        return f"{req.action} happens only through a card (invariant 7)"
    if req.action == "release" and agent:
        return '[money] out is "human": no agent releases money'
    if req.resource == "authority" and req.action == "confirm":
        if agent:
            return "agents never confirm an authority change; a person does"
        if req.proposer == who.name and policy.safeguards.authority_confirm != "self":
            return "the person who proposed an authority change does not confirm it"
    if req.resource == "authority" and req.action == "propose" and agent and who.reads_outside:
        return "an agent that reads outside documents never proposes an authority change"
    return ""


def _unknown(amount: Decimal | None, above: Decimal) -> bool:
    """An amount at or past ``above``, or no amount to check (counted as past)."""
    return amount is None or amount >= above


def _safeguards(sg: Safeguards, who: Principal, req: Request) -> Decision | str:
    """The shape's safeguards: a refusal reason, or the flags an allowed
    decision carries."""
    review_after = report = needs_second = False
    if req.action == "approve":
        if who.name in (req.submitter, req.payee):
            if sg.self_approval == "never":
                return "this tenant does not let anyone approve their own"
            if sg.self_approval == "below_limit" and _unknown(req.amount, sg.self_approval_limit):
                return f"approving your own is allowed only below {sg.self_approval_limit}"
            review_after = sg.review_after == "monthly"
            report = sg.report_self_approvals == "always" or (
                sg.report_self_approvals == "above_limit"
                and _unknown(req.amount, sg.self_approval_limit)
            )
        needs_second = sg.second_approver_above > 0 and (
            req.amount is None or req.amount > sg.second_approver_above
        )
    if req.action == "release" and req.approver == who.name:
        if sg.distinct_payer == "always":
            return "the approver does not also release the payment"
        if sg.distinct_payer == "above_limit" and (
            req.amount is None or req.amount > sg.distinct_payer_above
        ):
            return f"above {sg.distinct_payer_above} the approver does not also release the payment"
    return Decision(True, "", needs_second=needs_second, review_after=review_after, report=report)


def evaluate(policy: Policy, req: Request) -> Decision:
    """The floor, then forbids, then grants, then the shape's safeguards.
    Default deny: anything not granted is refused."""
    record = req.resource == "authority" and req.action in ("propose", "confirm")

    def deny(reason: str) -> Decision:
        return Decision(False, reason, record=record)

    who = policy.principal(req.principal)
    if who is None:
        return deny(f"{req.principal!r} is not a person or agent in authority.toml")
    if req.action not in ACTIONS or req.resource not in RESOURCES:
        return deny(f"{req.action}:{req.resource} is not an action on a resource")
    refused = _floor(policy, who, req)
    if refused:
        return deny(refused)
    for forbid in policy.forbids_of(who):
        if forbid.matches(req.action, req.resource) and _in_scope(forbid, who, req.scope):
            return deny(f"forbid {forbid.text!r} wins")
    granted = [
        g
        for g in policy.grants_of(who)
        if g.matches(req.action, req.resource)
        and _in_scope(g, who, req.scope)
        and _within_limit(g, req.amount)
    ]
    if not granted:
        return deny(f"no grant lets {who.name} {req.action} {req.resource} here")
    outcome = _safeguards(policy.safeguards, who, req)
    if isinstance(outcome, str):
        return deny(outcome)
    return replace(outcome, reason=f"granted by {granted[0].text!r}", record=record)


# ---- cards ----------------------------------------------------------------------


@dataclass(frozen=True)
class CardRule:
    """What deciding one card type is, declared by the agent that raises it
    (``CARD_AUTHORITY`` in its jobs.py). ``amount`` names the param holding
    the money the decision commits (``cents`` when it is whole cents);
    ``money=False`` says the card commits none. ``submitter`` names the
    param holding the person the card is about, for the self-approval
    safeguards."""

    action: str
    resource: str
    amount: str = ""
    cents: bool = False
    money: bool = True
    submitter: str = ""


UNDESCRIBED = CardRule("approve", "card")
"""A card type no agent described: only a full approver decides it."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def card_amount(rule: CardRule, params: dict) -> Decimal | None:
    """The money a card commits: zero for a card that commits none, None
    when a money card does not say (counted as past every limit)."""
    if not rule.money:
        return Decimal("0")
    raw = str(params.get(rule.amount, "")).replace("$", "").replace(",", "").strip()
    if not rule.amount or not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value / 100 if rule.cents else value


def card_request(rule: CardRule, principal: str, params: dict) -> Request:
    """The request deciding a card is evaluated as. Deciding happens on a
    card, so ``via_card`` is always true; a submitter is matched to people
    by their id (the slug of their name)."""
    submitter = _slug(str(params.get(rule.submitter, ""))) if rule.submitter else ""
    return Request(
        principal,
        rule.action,
        rule.resource,
        amount=card_amount(rule, params),
        submitter=submitter,
        via_card=True,
    )


# ---- changing authority -----------------------------------------------------------


def _covers(held: Permission, wanted: Permission) -> bool:
    return (
        held.action in ("*", wanted.action)
        and held.resource in ("*", wanted.resource)
        and (held.scope == "any" or wanted.scope == "own")
        and (held.limit is None or (wanted.limit is not None and wanted.limit <= held.limit))
    )


def may_grant(policy: Policy, name: str, perm: Permission) -> bool:
    """Nobody grants what they do not hold: ``name`` holds a grant that covers
    ``perm`` and no forbid that touches it. Agents grant nothing."""
    who = policy.principal(name)
    if who is None or who.kind == "agent":
        return False
    if any(_covers(f, perm) or _covers(perm, f) for f in policy.forbids_of(who)):
        return False
    return any(_covers(g, perm) for g in policy.grants_of(who))


def check_change(
    policy: Policy, *, proposer: str, confirmer: str, added: list[Permission]
) -> list[str]:
    """Why an authority change from ``proposer``, confirmed by
    ``confirmer``, that adds ``added`` must not take effect; empty when it
    may. Checked against the policy as it stands before the change."""
    problems: list[str] = []
    propose = evaluate(policy, Request(proposer, "propose", "authority"))
    if not propose.allowed:
        problems.append(f"{proposer} may not propose: {propose.reason}")
    confirm = evaluate(policy, Request(confirmer, "confirm", "authority", proposer=proposer))
    if not confirm.allowed:
        problems.append(f"{confirmer} may not confirm: {confirm.reason}")
    for perm in added:
        if not may_grant(policy, confirmer, perm):
            problems.append(f"{confirmer} does not hold {perm.text!r}, so cannot grant it")
    return problems


def change_record(*, proposer: str, confirmer: str, before: str, after: str) -> dict[str, str]:
    """What the ledger records for every authority change (the floor's third
    line): who proposed it, who confirmed it, and the file on each side."""
    return {
        "kind": "authority.changed",
        "proposer": proposer,
        "confirmer": confirmer,
        "before_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "after_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
    }


__all__ = [
    "ACTIONS",
    "AUTHORITY_CONFIRM",
    "DISTINCT_PAYER",
    "MONEY_OUT",
    "REPORT_SELF_APPROVALS",
    "RESOURCES",
    "REVIEW_AFTER",
    "SCOPES",
    "SELF_APPROVAL",
    "UNDESCRIBED",
    "AuthorityError",
    "CardRule",
    "Decision",
    "Permission",
    "Policy",
    "Principal",
    "Request",
    "Role",
    "Safeguards",
    "card_amount",
    "card_request",
    "change_record",
    "check_change",
    "evaluate",
    "may_grant",
    "parse_permission",
    "parse_policy",
]
