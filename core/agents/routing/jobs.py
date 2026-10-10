"""Routing: the clock behind cards on a route (#436; brief.md).

Only for a tenant with ``authority.toml`` (``ctx.authority``); for any other
tenant every job returns at once and records nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from ...authority import CardRule
from ...authority.routing import LEVELS, Delegation, check_delegation, level_due
from ...engine.authority_gate import load_delegations, waiting
from ...engine.clock import local_today
from ...engine.contracts import EventSpec, JobContext, JobHandler, JobOutput
from ...engine.runkey import RunKey

REMINDER_EVENT = "route.reminder"
DELEGATED_EVENT = "route.delegated"
REVOKED_EVENT = "route.revoked"


def _today(ctx: JobContext) -> date:
    return date.fromisoformat(
        str(ctx.params.get("today") or local_today(ctx.tenant.identity.timezone))
    )


def _sent(ctx: JobContext) -> set[tuple[int, int, int, str]]:
    rows = ctx.ledger.conn.execute(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant_slug, REMINDER_EVENT),
    ).fetchall()
    out = set()
    for r in rows:
        p = json.loads(r["payload_json"] or "{}")
        out.add((int(p["card"]), int(p["step"]), int(p["level"]), str(p["to"])))
    return out


def _doc(item: dict) -> str:
    return f"Card #{item['card']} ({item['action_type']})"


def _text(level: int, item: dict, to: str, covering: str = "") -> str:
    doc, due, card = _doc(item), item["due"], item["card"]
    if level == 1:
        return (
            f"{doc} is waiting for your decision by {due}. "
            f"`engine queue approve <tenant> --id {card} --as {to}` or reject."
        )
    if level == 2:
        return (
            f"{doc} still needs a decision by {due}. Want someone else to take it? "
            f"`engine queue handoff <tenant> --id {card} --as {to} --to NAME`, or say when "
            f"you'll get to it: `engine queue on-it <tenant> --id {card} --as {to} --by DATE`."
        )
    if level == 3:
        return f"Covering for {covering}: {doc} needs a decision by {due}."
    return f"{doc} needs a decision by {due} to stay on time."


def _tick_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "routing-tick")
    key.param("today")
    key.config("identity.timezone", "identity.shape")
    if ctx.authority is not None:
        key.value("authority", ctx.authority.sha256)
        key.rows(
            "pending",
            (
                (r["id"], json.dumps(r["params"], sort_keys=True))
                for r in ctx.ledger.list_approvals(ctx.tenant_slug, status="pending")
            ),
        )
        key.value("sent", sorted(_sent(ctx)))
        key.value("today", _today(ctx).isoformat())
    return key.digest()


def _tick_run(ctx: JobContext) -> JobOutput:
    shape = str(ctx.tenant.identity.shape or "")
    if ctx.authority is None:
        return JobOutput(status="ok", summary="routing: no authority.toml, nothing routes")
    policy = ctx.authority.policy
    today = _today(ctx)
    now = datetime.now(UTC).isoformat()
    sent = _sent(ctx)
    events: list[EventSpec] = []
    for item in waiting(policy, ctx.ledger, ctx.tenant_slug, today=today, now=now):
        level = level_due(item["route"], item["params"], policy.routing, today, shape)
        if level == 0:
            continue
        people = list(item["waiting_on"])
        targets: list[tuple[str, str]] = [(p, "") for p in people]
        if level == 3:
            targets = [
                (policy.people[p].backup, p)
                for p in people
                if p in policy.people and policy.people[p].backup
            ] or targets
        if level == 4:
            targets = [(p, "") for p in policy.holders(policy.routing.upward_role)] or targets
        step = item["route"].index
        for to, covering in targets:
            if (item["card"], step, level, to) in sent:
                continue
            payload = {
                "card": item["card"],
                "step": step,
                "level": level,
                "kind": LEVELS[level - 1],
                "to": to,
                "due": item["due"],
                "text": _text(level, item, to, covering),
            }
            events.append(
                EventSpec(
                    key=f"rem:{item['card']}:{step}:{level}:{to}",
                    event_type=REMINDER_EVENT,
                    payload=payload,
                )
            )
    return JobOutput(
        status="ok",
        summary=f"routing: {len(events)} reminder(s) recorded",
        events=events,
    )


def _delegation_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "routing-delegate")
    for name in ("by", "to", "role", "start", "until", "delegation", "today"):
        key.param(name)
    key.config("identity.timezone")
    if ctx.authority is not None:
        key.value("authority", ctx.authority.sha256)
    return key.digest()


def _delegate_run(ctx: JobContext) -> JobOutput:
    if ctx.authority is None:
        return JobOutput(status="error", summary="routing: no authority.toml, nothing to delegate")
    today = _today(ctx)
    p = ctx.params
    try:
        d = Delegation(
            str(p.get("by", "")),
            str(p.get("to", "")),
            str(p.get("role", "")),
            date.fromisoformat(str(p.get("start") or today.isoformat())),
            date.fromisoformat(str(p.get("until", ""))),
        )
    except ValueError as exc:
        return JobOutput(status="error", summary=f"routing: a delegation needs dates: {exc}")
    refusal = check_delegation(ctx.authority.policy, d, today)
    if refusal:
        return JobOutput(status="error", summary=f"routing: delegation refused: {refusal}")
    did = f"{d.frm}:{d.to}:{d.role}:{d.start.isoformat()}"
    payload = {
        "delegation": did,
        "by": d.frm,
        "to": d.to,
        "role": d.role,
        "start": d.start.isoformat(),
        "until": d.until.isoformat(),
    }
    return JobOutput(
        status="ok",
        summary=f"routing: {d.frm} hands {d.role} to {d.to} until {d.until.isoformat()}",
        events=[EventSpec(key=f"delegated:{did}", event_type=DELEGATED_EVENT, payload=payload)],
    )


def _revoke_run(ctx: JobContext) -> JobOutput:
    if ctx.authority is None:
        return JobOutput(status="error", summary="routing: no authority.toml, nothing to revoke")
    did, by = str(ctx.params.get("delegation", "")), str(ctx.params.get("by", ""))
    live = {
        f"{d.frm}:{d.to}:{d.role}:{d.start.isoformat()}": d
        for d in load_delegations(ctx.ledger, ctx.tenant_slug)
    }
    if did not in live:
        return JobOutput(status="error", summary=f"routing: no delegation {did!r} to revoke")
    if live[did].frm != by:
        return JobOutput(status="error", summary="routing: only the person who gave it revokes it")
    return JobOutput(
        status="ok",
        summary=f"routing: delegation {did} revoked",
        events=[
            EventSpec(
                key=f"revoked:{did}",
                event_type=REVOKED_EVENT,
                payload={"delegation": did, "by": by},
            )
        ],
    )


JOBS: dict[str, JobHandler] = {
    "tick": JobHandler(key=_tick_key, run=_tick_run),
    "delegate": JobHandler(key=_delegation_key, run=_delegate_run),
    "revoke": JobHandler(key=_delegation_key, run=_revoke_run),
}

# Routing raises no cards (#435 declares what each card is).
CARD_AUTHORITY: dict[str, CardRule] = {}
