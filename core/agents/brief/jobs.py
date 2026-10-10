"""Brief agent jobs: weekly.

Tim's first voice (Ask Tim build order #2): one plain page a week that leads
with what needs the person, then what is coming, the money, and whether the
engine itself is healthy. Boundary classification (docs/boundary-rules.md):
**code**. Every fact comes from the read-only tools in core/tools; this module
only chooses and arranges. No model is on this path; a model voice can sit on
top later without changing a figure. Sending the page is an external act, so
it waits for an approved card unless the tenant's own policy names the send
as unattended ([brief].unattended = ["send"]).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ...authority import CardRule
from ...engine.authority_gate import lane_decision, waiting
from ...engine.clock import local_today
from ...engine.config import tenant_dir
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobHandler, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ...tools.catalog import Tools

SEND_ACTION = "brief.send"
WRITTEN_EVENT = "brief.written"
SENT_EVENT = "brief.sent"
FINAL_WEEK_DAYS = 7
SHOW_PAYABLES = 5


def iso_week(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _day(iso: str | None) -> str:
    if not iso:
        return "no date"
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d:%b %Y}"


def _usd(amount: str | None) -> str:
    return f"${Decimal(amount or '0'):,.2f}"


# ---- inputs -------------------------------------------------------------------------


def _today(ctx: JobContext) -> date:
    given = ctx.params.get("today")
    return date.fromisoformat(given or local_today(ctx.tenant.identity.timezone))


def _list(ctx: JobContext, param: str, configured: list[str]) -> list[str]:
    given = ctx.params.get(param)
    if given is None:
        return list(configured)
    return [x.strip() for x in str(given).split(",") if x.strip()]


def _dir(ctx: JobContext) -> Path | None:
    configured = ctx.params.get("dir") or ctx.tenant.brief.dir
    return Path(configured) if configured else None


def _obligations(ctx: JobContext) -> Path:
    given = ctx.params.get("obligations_file") or ctx.tenant.deadlines.file
    return Path(given) if given else tenant_dir(ctx.tenant_slug) / "obligations.toml"


def _card(ctx: JobContext, week: str) -> tuple[str, int | None, dict]:
    """The newest send card for this week: (status, id, params)."""
    rows = ctx.ledger.conn.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "ORDER BY id DESC",
        (ctx.tenant_slug, SEND_ACTION),
    ).fetchall()
    for row in rows:
        params = json.loads(row["params_json"] or "{}")
        if params.get("week") == week:
            return str(row["status"]), int(row["id"]), params
    return "none", None, {}


def _sent(ctx: JobContext, week: str) -> bool:
    rows = ctx.ledger.conn.execute(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant_slug, SENT_EVENT),
    ).fetchall()
    return any(json.loads(r["payload_json"] or "{}").get("week") == week for r in rows)


def _key(ctx: JobContext) -> str:
    key = RunKey(ctx, "brief-weekly")
    for name in ("today", "dir", "recipients", "unattended", "obligations_file"):
        key.param(name)
    key.config("brief", "deadlines", "identity.timezone", "identity.legal_name")
    week = iso_week(_today(ctx))
    status, card_id, _ = _card(ctx, week)
    key.value("week", week)
    key.value("card", [status, card_id, _sent(ctx, week)])
    if ctx.authority is not None:
        key.value("authority", ctx.authority.sha256)
    return key.digest()


# ---- the facts and the page ------------------------------------------------------


def gather(tools: Tools) -> dict:
    deadlines = [r for r in tools.call("deadlines", {"days": 3650})["rows"] if r["window"]]
    expenses = [
        r for r in tools.call("expense_reports", {"limit": 50})["rows"] if not r["cleared_date"]
    ]
    runs = tools.call("recent_runs", {"limit": 500})["rows"]
    return {
        "as_of": tools._today().isoformat(),
        "cards": tools.call("waiting_cards", {"limit": 50})["rows"],
        "deadlines": deadlines,
        "payables": tools.call("open_payables", {"limit": 200}),
        "expenses": expenses,
        "runs": [r for r in runs if r["status"] == "error"],
        "sealed": tools.call("close_status", {"limit": 1})["last_locked_month"],
    }


def _deadline_line(d: dict) -> str:
    label = f"{d['title']} ({d['who']})" if d.get("who") else d["title"]
    left = d["days_left"]
    when = f"{-left} days overdue" if left < 0 else ("today" if left == 0 else f"in {left} days")
    return f"- {label}: due {_day(d['due'])}, {when}"


def render(facts: dict, *, name: str) -> str:
    urgent = [d for d in facts["deadlines"] if d["days_left"] <= FINAL_WEEK_DAYS]
    later = [d for d in facts["deadlines"] if d["days_left"] > FINAL_WEEK_DAYS]
    out = [f"# Your week, {_day(facts['as_of'])}", ""]

    out += ["## Needs you", ""]
    needs = [
        f"- Card #{c['card']}: {c['action_type']}, waiting since {_day(c['created_at'])}"
        for c in facts["cards"]
    ] + [_deadline_line(d) for d in urgent]
    out += needs or ["Nothing is waiting on you this week."]

    if facts.get("waiting"):
        # Under authority.toml with a process owner (#436): their page only.
        out += ["", "## Waiting on others", ""]
        out += [
            f"- Card #{w['card']}: {w['action_type']}, {w['step']}, waiting on "
            f"{', '.join(w['waiting_on']) or 'no one named'}, by {_day(w['due'])}"
            for w in facts["waiting"]
        ]

    out += ["", "## Coming up", ""]
    out += [_deadline_line(d) for d in later] or [
        "Nothing else falls due inside its reminder window."
    ]

    out += ["", "## Money", ""]
    pay = facts["payables"]["rows"]
    if pay:
        noun = "payable" if len(pay) == 1 else "payables"
        out.append(f"- {len(pay)} open {noun}, {_usd(facts['payables']['total'])} in all")
        for p in pay[:SHOW_PAYABLES]:
            if p.get("status") == "Scheduled":
                when = (
                    f", scheduled {_day(p['payment_date'])}"
                    if p.get("payment_date")
                    else ", scheduled"
                )
            else:
                when = f", due {_day(p['due_date'])}" if p.get("due_date") else ""
            out.append(f"  - {p['vendor']} {p['invoice_number']}, {_usd(p['amount'])}{when}")
    else:
        out.append("- No open payables.")
    for e in facts["expenses"]:
        out.append(
            f"- Expense report, {e['person']} {e['month']}: {_usd(e['total'])}, {e['status']}"
        )

    out += ["", "## The engine", ""]
    if facts["runs"]:
        out += [
            f"- {r['agent']}/{r['job']} ended in error: {r['summary'][:120]}" for r in facts["runs"]
        ]
    else:
        out.append("- Every scheduled job's last run ended clean.")
    if facts["sealed"]:
        month = date.fromisoformat(f"{facts['sealed']}-01")
        out.append(f"- The books are sealed through {month:%B %Y}.")
    else:
        out.append("- No month has been sealed in the engine yet.")

    out += ["", f"From Tim, {name}'s back office. Every figure here comes from the books.", ""]
    return "\n".join(out)


# ---- the job ------------------------------------------------------------------------


def _send_client(ctx: JobContext):
    """Factory hook (evals replace it). The token is fetched here, before any
    stamp, so an auth failure attempts nothing (the statements rule, #135)."""
    from ...adapters.mail import client_for

    return client_for(ctx.tenant.mail, eager=True)


def _write(ctx: JobContext, path: Path, text: str) -> None:
    ctx.guard.check_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _stamp(ctx: JobContext, card_id: int, params: dict) -> None:
    params = {**params, "send_started": datetime.now(UTC).isoformat()}
    ctx.ledger.conn.execute(
        "UPDATE approval_queue SET params_json = ? WHERE id = ?",
        (json.dumps(params, sort_keys=True), card_id),
    )
    ctx.ledger.conn.commit()


def _weekly_run(ctx: JobContext) -> JobOutput:
    today = _today(ctx)
    week = iso_week(today)
    recipients = _list(ctx, "recipients", ctx.tenant.brief.recipients)
    unattended = _list(ctx, "unattended", ctx.tenant.brief.unattended)
    folder = _dir(ctx)
    path = folder / f"brief-{week}.md" if folder else None

    events: list[EventSpec] = []
    actions: list[str] = []
    if path is not None and path.is_file():
        page = path.read_text(encoding="utf-8")  # the week's page is Monday's page
    else:
        tools = Tools(
            ctx.tenant_slug,
            ledger_root=ctx.ledger.root,
            obligations_file=_obligations(ctx),
            today=today.isoformat(),
            lead_days=tuple(ctx.tenant.deadlines.lead_days),
        )
        facts = gather(tools)
        if ctx.authority is not None and ctx.authority.policy.routing.owner:
            facts["waiting"] = [
                {k: w[k] for k in ("card", "action_type", "step", "waiting_on", "due")}
                for w in waiting(
                    ctx.authority.policy,
                    ctx.ledger,
                    ctx.tenant_slug,
                    today=today,
                    now=datetime.now(UTC).isoformat(),
                )
            ]
        page = render(facts, name=ctx.tenant.identity.legal_name)
        if not ctx.shadow:
            if path is not None:
                _write(ctx, path, page)
                actions.append(f"brief written: {path.name}")
            events.append(
                EventSpec(
                    key=f"written:{week}",
                    event_type=WRITTEN_EVENT,
                    payload={
                        "week": week,
                        "path": str(path) if path else "",
                        "needs": len(facts["cards"])
                        + sum(1 for d in facts["deadlines"] if d["days_left"] <= FINAL_WEEK_DAYS),
                    },
                )
            )
    summary = f"brief {week}: " + ("written" if events else "unchanged")
    if not recipients or ctx.shadow:
        return JobOutput(status="ok", summary=summary, actions=actions, events=events)

    status, card_id, params = _card(ctx, week)
    if _sent(ctx, week):
        return JobOutput(status="ok", summary=f"{summary}; already sent", events=events)
    if status == "approved" and params.get("send_started"):
        return JobOutput(
            status="ok",
            summary=f"{summary}; a send for card #{card_id} started with no outcome recorded",
            events=events,
            anomalies=[
                Anomaly(
                    code="brief.send_unconfirmed",
                    detail=f"card #{card_id}: check Sent Items; never resent automatically",
                )
            ],
        )
    send_now = "send" in unattended
    granted = None
    if ctx.authority is not None:
        # authority.toml is the only source (#435): the lane's agent sends
        # with no one asked only when it holds send:message, and never
        # decides a card a person was already asked.
        granted = lane_decision(ctx.authority.policy, "brief", "send", "message")
        send_now = granted is not None and status == "none"
    if status == "approved" or send_now:
        mailer = _send_client(ctx)
        if card_id is not None:
            _stamp(ctx, card_id, params)
        mailer.send_mail(
            subject=f"{ctx.tenant.identity.legal_name}: your week, {_day(today.isoformat())}",
            body=page,
            to=recipients,
        )
        events.append(
            EventSpec(
                key=f"sent:{week}",
                event_type=SENT_EVENT,
                payload={"week": week, "card_id": card_id, "recipients": recipients},
            )
        )
        decided = []
        if granted is not None and card_id is None:
            agent, verdict = granted
            decided = [
                _send_card(week, path, recipients, decided_by=agent, decided_reason=verdict.reason)
            ]
        return JobOutput(
            status="ok",
            summary=f"{summary}; sent to {', '.join(recipients)}",
            actions=[*actions, f"sent to {', '.join(recipients)}"],
            events=events,
            approvals=decided,
        )
    if status in ("pending", "rejected"):
        return JobOutput(status="ok", summary=f"{summary}; send card {status}", events=events)
    return JobOutput(
        status="needs_approval",
        summary=f"{summary}; send approval card parked",
        actions=actions,
        events=events,
        approvals=[_send_card(week, path, recipients)],
    )


def _send_card(week: str, path, recipients: list[str], **decided: str) -> ApprovalSpec:
    return ApprovalSpec(
        key=f"brief:{week}",
        action_type=SEND_ACTION,
        params={
            "week": week,
            "path": str(path) if path else "",
            "recipients": ", ".join(recipients),
        },
        reason=f"email this week's brief to {', '.join(recipients)}",
        **decided,
    )


JOBS: dict[str, JobHandler] = {"weekly": JobHandler(key=_key, run=_weekly_run)}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {SEND_ACTION: CardRule("send", "message", money=False)}
