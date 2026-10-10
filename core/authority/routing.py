"""A document on its route (docs/tenant-kit-design.md, section 3; #436).

Pure functions over the policy and one card's route state; the engine
(``core/engine/authority_gate.py``) reads and writes that state on the card,
and the ``routing`` lane keeps the clock.

- **One document, never copies.** A route lives on its card; every approval
  is a vote recorded on that card, and the card resolves when the last step
  is done.
- **Steps name roles.** ``one`` approver of the role, ``two`` distinct ones,
  or ``all`` who hold it. No route in the file: one step, and a second
  distinct approver past the second-approver line.
- **Delegation with an end date** (checked against SAP Concur's published
  delegate rules; a broader survey is #455): a person hands a role they hold directly to another
  person until a date; it lapses on its own; a delegate acts on the
  delegator's behalf and never on their own submission, and never passes
  the role on again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from . import CardRule, Decision, Policy, Request, evaluate
from .routes import Routing, Step

MAX_DELEGATION_DAYS = 90
"""Longest a delegation runs before it must be given again."""


@dataclass(frozen=True)
class Delegation:
    frm: str
    to: str
    role: str
    start: date
    until: date

    def active(self, today: date) -> bool:
        return self.start <= today <= self.until


def check_delegation(policy: Policy, d: Delegation, today: date) -> str:
    """Why this delegation must not be given, or ""."""
    frm, to = policy.people.get(d.frm), policy.people.get(d.to)
    if frm is None:
        return f"{d.frm!r} is not a person in authority.toml; only a person delegates"
    if to is None:
        return f"{d.to!r} is not a person in authority.toml; a role goes to a person"
    if d.frm == d.to:
        return "a delegation goes to someone else"
    if d.role not in frm.roles:
        return f"{d.frm} does not hold {d.role!r} directly, so cannot hand it on"
    if d.until < today or d.until < d.start:
        return "a delegation ends on a date still to come"
    if (d.until - d.start).days > MAX_DELEGATION_DAYS:
        return f"a delegation runs at most {MAX_DELEGATION_DAYS} days; give it again after"
    return ""


def needs_second(policy: Policy, rule: CardRule, amount: Decimal | None) -> bool:
    line = policy.safeguards.second_approver_above
    return rule.action == "approve" and line > 0 and (amount is None or amount > line)


def route_for(policy: Policy, rule: CardRule, amount: Decimal | None) -> tuple[Step, ...]:
    """The steps a card takes: the most specific route for its resource and
    amount, else one step; a second distinct approver past the line."""
    matches = [
        r
        for r in policy.routing.routes
        if r.resource in ("*", rule.resource)
        and (r.above == 0 or amount is None or amount > r.above)
    ]
    matches.sort(key=lambda r: (r.resource != "*", r.above))
    steps = matches[-1].steps if matches else (Step(),)
    if needs_second(policy, rule, amount) and sum(_needed(policy, s) for s in steps) < 2:
        steps = (Step(need="two"),) if not matches else (*steps, Step())
    return steps


def _needed(policy: Policy, step: Step) -> int:
    if step.need == "two":
        return 2
    if step.need == "all":
        return max(1, len(policy.holders(step.role)))
    return 1


# ---- the route on a card ----------------------------------------------------------


@dataclass
class CardRoute:
    """A card's route state, kept in its params as strings."""

    steps: tuple[Step, ...]
    index: int = 0
    votes: list[tuple[int, str, str]] = field(default_factory=list)  # step, actor, authority
    opened: str = ""
    handoff_to: str = ""
    on_it_until: str = ""
    on_it_set: str = ""

    @property
    def step(self) -> Step:
        return self.steps[self.index]

    @property
    def done(self) -> bool:
        return self.index >= len(self.steps)

    def voted(self, step: int | None = None) -> list[tuple[str, str]]:
        at = self.index if step is None else step
        return [(actor, auth) for s, actor, auth in self.votes if s == at]

    @classmethod
    def from_params(cls, params: dict) -> CardRoute | None:
        if "route" not in params:
            return None
        return cls(
            steps=tuple(Step(r, n) for r, n in json.loads(params["route"])),
            index=int(params.get("route_step", "0")),
            votes=[tuple(v) for v in json.loads(params.get("route_votes", "[]"))],
            opened=str(params.get("route_opened", "")),
            handoff_to=str(params.get("handoff_to", "")),
            on_it_until=str(params.get("on_it_until", "")),
            on_it_set=str(params.get("on_it_set", "")),
        )

    def to_params(self) -> dict[str, str]:
        out = {
            "route": json.dumps([[s.role, s.need] for s in self.steps]),
            "route_step": str(self.index),
            "route_votes": json.dumps([list(v) for v in self.votes]),
            "route_opened": self.opened,
        }
        for key in ("handoff_to", "on_it_until", "on_it_set"):
            out[key] = getattr(self, key)
        return out


def authority_for(
    policy: Policy, actor: str, step: Step, delegations: list[Delegation], today: date
) -> str | None:
    """Whose authority ``actor`` uses on this step: their own when they hold
    the role (or the step names none), the delegator's under an active
    delegation, else None."""
    who = policy.principal(actor)
    if who is None:
        return None
    if not step.role or step.role in who.roles:
        return actor
    for d in delegations:
        if d.to == actor and d.role == step.role and d.active(today):
            frm = policy.people.get(d.frm)
            if frm is not None and step.role in frm.roles:
                return d.frm
    return None


def step_done(policy: Policy, route: CardRoute) -> bool:
    step = route.step
    authorities = {auth for _actor, auth in route.voted()}
    if step.need == "all" and step.role:
        holders = set(policy.holders(step.role))
        return holders <= authorities if holders else bool(authorities)
    return len(authorities) >= _needed(policy, step)


def can_decide(policy: Policy, rule: CardRule, resource_actor: str) -> bool:
    """Whether ``resource_actor`` holds any grant that decides this kind of
    card (for listing who a role-less step waits on)."""
    who = policy.principal(resource_actor)
    return who is not None and any(
        g.matches(rule.action, rule.resource) for g in policy.grants_of(who)
    )


def waiting_on(
    policy: Policy, rule: CardRule, route: CardRoute, delegations: list[Delegation], today: date
) -> list[str]:
    """The people this card's current step is waiting on: the handoff when
    there is one, else the step's role holders who have not decided, each
    replaced by their active delegate."""
    if route.done:
        return []
    if route.handoff_to:
        return [route.handoff_to]
    step = route.step
    voted = {auth for _actor, auth in route.voted()}
    if step.role:
        holders = policy.holders(step.role)
    else:
        holders = [p for p in policy.people if can_decide(policy, rule, p)]
    out: list[str] = []
    for name in holders:
        if name in voted:
            continue
        delegate = (
            next(
                (
                    d.to
                    for d in delegations
                    if d.frm == name and d.role == step.role and d.active(today)
                ),
                None,
            )
            if step.role
            else None
        )
        out.append(delegate or name)
    return list(dict.fromkeys(out))


# ---- the clock ------------------------------------------------------------------------


def due_date(route: CardRoute, params: dict, routing: Routing) -> date:
    """The document's real deadline where the card names one, else the
    step's window from when it opened; an "on it by" pause pushes it by the
    length of the pause."""
    opened = date.fromisoformat(route.opened[:10])
    deadline = ""
    for key in ("due_date", "due", "deadline"):
        if params.get(key):
            deadline = str(params[key])[:10]
            break
    try:
        due = date.fromisoformat(deadline) if deadline else opened + timedelta(routing.window_days)
    except ValueError:
        due = opened + timedelta(routing.window_days)
    if route.on_it_until and route.on_it_set:
        pause = date.fromisoformat(route.on_it_until) - date.fromisoformat(route.on_it_set[:10])
        due += max(pause, timedelta(0))
    return due


LEVELS = ("reminder", "handoff_offer", "covering_for", "upward")


def level_due(route: CardRoute, params: dict, routing: Routing, today: date, shape: str) -> int:
    """The highest escalation level due today (0 = none): 1 a reminder to
    the approver, 2 the reminder that offers a handoff, 3 the backup
    "covering for" (only when turned on), 4 upward (organization shape
    only, when turned on). Nothing while an "on it by" date is ahead."""
    if route.on_it_until and today < date.fromisoformat(route.on_it_until):
        return 0
    due = due_date(route, params, routing)
    level = 0
    for n, days in ((1, routing.first_reminder_days), (2, routing.second_reminder_days)):
        if today >= due - timedelta(routing.window_days - days):
            level = n
    if routing.backup and today >= due - timedelta(routing.window_days - routing.backup_after_days):
        level = 3
    if routing.upward and shape.endswith("-organization") and today >= due:
        level = 4
    return level


# ---- one decision on a route ----------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    refusal: str = ""
    route: CardRoute | None = None
    final: bool = False
    authority: str = ""
    verdict: Decision | None = None


def decide(
    policy: Policy,
    rule: CardRule,
    request: Request,
    route: CardRoute,
    *,
    decision: str,
    delegations: list[Delegation],
    today: date,
    now: str,
) -> Outcome:
    """One approval or rejection by ``request.principal`` on the card's
    current step. A rejection by anyone who may decide the step ends the
    route; an approval is a vote, and the card resolves when the last step
    has its approvers."""
    actor = request.principal
    step = route.step
    authority = authority_for(policy, actor, step, delegations, today)
    if authority is None:
        return Outcome(
            refusal=f"this step waits on {step.role or 'an approver'}, which {actor} does not hold"
        )
    if authority != actor and request.submitter and actor == request.submitter:
        return Outcome(refusal="a delegate never decides their own submission")
    verdict = evaluate(policy, replace(request, principal=authority))
    if not verdict.allowed:
        return Outcome(refusal=verdict.reason)
    if decision == "rejected":
        return Outcome(route=route, final=True, authority=authority, verdict=verdict)
    done_by = {a for _s, a, _auth in route.votes} | {auth for _s, _a, auth in route.votes}
    if actor in done_by or authority in done_by:
        return Outcome(
            refusal=f"{actor} already approved this card; the next yes is someone else's"
        )
    votes = [*route.votes, (route.index, actor, authority)]
    moved = replace(route, votes=votes, handoff_to="")
    if step_done(policy, moved):
        moved = replace(moved, index=moved.index + 1, opened=now, on_it_until="", on_it_set="")
    return Outcome(route=moved, final=moved.done, authority=authority, verdict=verdict)


__all__ = [
    "LEVELS",
    "MAX_DELEGATION_DAYS",
    "CardRoute",
    "Delegation",
    "Outcome",
    "authority_for",
    "check_delegation",
    "decide",
    "due_date",
    "level_due",
    "needs_second",
    "route_for",
    "step_done",
    "waiting_on",
]
