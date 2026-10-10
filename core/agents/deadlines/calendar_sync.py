"""deadlines/calendar: write each open deadline into the person's own calendar.

The calendar file carries dates, but Outlook and Google drop the reminders in
it, so the engine writes the events itself (the Ask Tim design note,
2026-10-04). Pure code end to end:

- Which calendar: :func:`detect_provider`, in a fixed order (the tenant's
  setting; the organization's Microsoft mail connection; the address's domain;
  its public mail records; else the calendar file).
- What to write: one all-day event per open occurrence, carrying the final
  week's reminder (Google allows at most four weeks ahead and Outlook one
  reminder, so the earlier nudges ride in the weekly brief).
- What changed: the ledger keeps every write (``deadlines.calendar`` events);
  the plan creates what is new, updates what changed, and removes what is
  done or moved. Nothing changed, nothing written.
- Writing to someone's calendar is an external act (boundary rule 4): the
  plan waits for an approved card unless the tenant's own policy names the
  calendar unattended (``[deadlines].unattended = ["calendar"]``), which is
  the one-time grant at setup.

v1 writes Microsoft calendars; Google is detected and falls back to the file
until a Google sign-in exists.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import Callable
from datetime import date, timedelta

from ...engine.authority_gate import lane_decision
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from .jobs import _label, _ledger_state, _obligations_path, _today, open_occurrences
from .schema import load_obligations

CAL_EVENT = "deadlines.calendar"
CAL_ACTION = "deadlines.calendar_sync"
FINAL_WEEK_DAYS = 7
WRITES = {"graph"}
CAL_SCOPES = ("Calendars.ReadWrite",)
"""Asked for on its own, never added to [mail].scopes: a mail job whose cached
consent lacks the calendar scope would otherwise fail every silent token."""

GOOGLE_DOMAINS = {"gmail.com", "googlemail.com"}
MICROSOFT_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
APPLE_DOMAINS = {"icloud.com", "me.com", "mac.com"}


# ---- which calendar ---------------------------------------------------------------


def mx_hosts_from_doh(answer: dict) -> list[str]:
    hosts = []
    for rec in answer.get("Answer") or []:
        if rec.get("type") == 15:
            parts = str(rec.get("data", "")).split()
            if parts:
                hosts.append(parts[-1].rstrip(".").lower())
    return hosts


def doh_mx(domain: str) -> list[str]:
    """The domain's public mail records, over DNS-over-HTTPS (standard library)."""
    req = urllib.request.Request(
        f"https://cloudflare-dns.com/dns-query?name={domain}&type=MX",
        headers={"Accept": "application/dns-json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return mx_hosts_from_doh(json.loads(resp.read()))


def detect_provider(
    *,
    setting: str,
    mail_client_id: str,
    address: str,
    mx_lookup: Callable[[str], list[str]] | None = doh_mx,
) -> tuple[str, str]:
    """(provider, why): graph, google, ics or off."""
    if setting:
        return setting, "the tenant's setting"
    if mail_client_id:
        return "graph", "the organization's Microsoft mail connection"
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    if domain in GOOGLE_DOMAINS:
        return "google", f"{domain} is Google"
    if domain in MICROSOFT_DOMAINS:
        return "graph", f"{domain} is Microsoft"
    if domain in APPLE_DOMAINS:
        return "ics", f"{domain} calendars take the calendar file"
    if domain and mx_lookup is not None:
        try:
            hosts = mx_lookup(domain)
        except OSError:
            hosts = []
        if any(h.endswith("mail.protection.outlook.com") for h in hosts):
            return "graph", f"{domain}'s mail records name Microsoft 365"
        if any(h.endswith("google.com") or h.endswith("googlemail.com") for h in hosts):
            return "google", f"{domain}'s mail records name Google Workspace"
    return "ics", "no calendar service detected; the calendar file covers it"


def reminder_minutes(leads: list[int]) -> int:
    """The final week's reminder: the smallest lead, never more than a week."""
    return min(min(leads, default=0), FINAL_WEEK_DAYS) * 24 * 60


def next_day(d: date) -> date:
    return d + timedelta(days=1)


# ---- the plan ----------------------------------------------------------------------


def _param_list(ctx: JobContext, name: str, configured: list[str]) -> list[str]:
    given = ctx.params.get(name)
    if given is None:
        return list(configured)
    return [x.strip() for x in str(given).split(",") if x.strip()]


def _provider(ctx: JobContext) -> tuple[str, str]:
    setting = ctx.params.get("calendar")
    if setting is None:
        setting = ctx.tenant.deadlines.calendar
    address = ctx.tenant.deadlines.calendar_account or ctx.tenant.mail.keychain_account
    return detect_provider(
        setting=str(setting), mail_client_id=ctx.tenant.mail.client_id, address=address
    )


def _synced(ctx: JobContext) -> dict[str, dict]:
    """uid -> the latest write for it (deleted ones dropped)."""
    live: dict[str, dict] = {}
    for e in ctx.ledger.read_event_log():
        if e.get("event_type") != CAL_EVENT:
            continue
        p = e.get("payload") or {}
        if p.get("op") == "deleted":
            live.pop(p.get("uid"), None)
        else:
            live[p.get("uid")] = p
    return live


def _desired(ctx: JobContext) -> dict[str, dict]:
    path = _obligations_path(ctx)
    if not path.is_file():
        return {}
    done, _ = _ledger_state(ctx)
    today = _today(ctx)
    out: dict[str, dict] = {}
    for ob in load_obligations(path):
        leads = ob.leads(ctx.tenant.deadlines.lead_days)
        for due in open_occurrences(ob, today=today, leads=leads, done=done):
            body = "\n".join(
                p
                for p in (
                    ob.kind and f"Kind: {ob.kind}",
                    ob.notes,
                    ob.evidence and f"Evidence: {ob.evidence}",
                )
                if p
            )
            item = {
                "uid": f"{ob.id}-{due:%Y%m%d}",
                "subject": _label(ob),
                "day": due.isoformat(),
                "body": body,
                "reminder": reminder_minutes(leads),
            }
            item["fp"] = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16]
            out[item["uid"]] = item
    return out


def plan(desired: dict[str, dict], synced: dict[str, dict]) -> list[dict]:
    ops: list[dict] = []
    for uid, s in sorted(synced.items()):
        if uid not in desired:
            ops.append(
                {
                    "op": "delete",
                    "uid": uid,
                    "event_id": s["event_id"],
                    "subject": s.get("subject", ""),
                }
            )
    for uid, d in sorted(desired.items(), key=lambda kv: (kv[1]["day"], kv[0])):
        if uid not in synced:
            ops.append({"op": "create", **d})
        elif synced[uid].get("fp") != d["fp"]:
            ops.append({"op": "update", "event_id": synced[uid]["event_id"], **d})
    return ops


def _plan_hash(ops: list[dict]) -> str:
    return hashlib.sha256(json.dumps(ops, sort_keys=True).encode()).hexdigest()[:16]


def _card(ctx: JobContext, plan_hash: str) -> tuple[str, int | None]:
    rows = ctx.ledger.conn.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "ORDER BY id DESC",
        (ctx.tenant_slug, CAL_ACTION),
    ).fetchall()
    for r in rows:
        if json.loads(r["params_json"] or "{}").get("plan") == plan_hash:
            return str(r["status"]), int(r["id"])
    return "none", None


# ---- the job --------------------------------------------------------------------------


def _calendar_client(ctx: JobContext):
    """Factory hook (evals replace it). The token is fetched here, before any
    write, so a missing calendar permission attempts nothing."""
    from ...adapters.graph_calendar import GraphCalendarClient
    from ...adapters.graph_mail import keychain_token_provider

    mail = ctx.tenant.mail
    token = keychain_token_provider(
        client_id=mail.client_id,
        tenant_id=mail.tenant_id,
        scopes=list(CAL_SCOPES),
        keychain_service=mail.keychain_service,
        keychain_account=mail.keychain_account,
    )()
    return GraphCalendarClient(token_provider=lambda: token)


def calendar_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "deadlines-calendar")
    for name in ("obligations_file", "today", "calendar", "unattended"):
        key.param(name)
    key.config("deadlines", "mail", "identity.timezone")
    ops = plan(_desired(ctx), _synced(ctx))
    h = _plan_hash(ops)
    key.value("plan", h)
    key.value("card", list(_card(ctx, h)))
    if ctx.authority is not None:
        key.value("authority", ctx.authority.sha256)
    return key.digest()


def _counts(ops: list[dict]) -> str:
    parts = [
        f"{sum(1 for o in ops if o['op'] == k)} to {k}" for k in ("create", "update", "delete")
    ]
    return ", ".join(p for p in parts if not p.startswith("0 "))


def calendar_run(ctx: JobContext) -> JobOutput:
    from ...adapters.graph_mail import GraphAuthError, GraphMailError

    provider, why = _provider(ctx)
    if provider not in WRITES:
        return JobOutput(
            status="ok",
            summary=f"deadlines calendar: {provider} ({why}); nothing written, "
            "the calendar file covers it",
        )
    ops = plan(_desired(ctx), _synced(ctx))
    if not ops:
        return JobOutput(status="ok", summary="deadlines calendar: in step, nothing to write")
    h = _plan_hash(ops)
    if ctx.shadow:
        n = {k: sum(1 for o in ops if o["op"] == k) for k in ("create", "update", "delete")}
        return JobOutput(
            status="ok",
            summary=f"deadlines calendar: would create {n['create']}, update {n['update']}, "
            f"delete {n['delete']}",
        )
    unattended = "calendar" in _param_list(ctx, "unattended", ctx.tenant.deadlines.unattended)
    status, card_id = _card(ctx, h)
    granted = None
    if ctx.authority is not None:
        # authority.toml is the only source (#435): the lane's agent writes
        # with no one asked only when it holds send:calendar, and never
        # decides a card a person was already asked.
        granted = lane_decision(ctx.authority.policy, "deadlines", "send", "calendar")
        unattended = granted is not None and status == "none"
    if not unattended and status != "approved":
        if status in ("pending", "rejected"):
            return JobOutput(
                status="ok", summary=f"deadlines calendar: plan card #{card_id} {status}"
            )
        return JobOutput(
            status="needs_approval",
            summary=f"deadlines calendar: {_counts(ops)}; approval card parked",
            approvals=[_plan_card(ops, h, provider)],
        )
    try:
        client = _calendar_client(ctx)
    except GraphAuthError as exc:
        return JobOutput(
            status="ok",
            summary="deadlines calendar: no calendar permission yet; nothing written",
            anomalies=[
                Anomaly(
                    code="deadlines.calendar_permission",
                    detail="grant the calendar once: "
                    f"`engine mail consent {ctx.tenant_slug} --scope Calendars.ReadWrite` "
                    f"([mail].scopes stays as it is): {exc}",
                )
            ],
        )
    tz = ctx.tenant.identity.timezone
    events: list[EventSpec] = []
    anomalies: list[Anomaly] = []
    for o in ops:
        try:
            if o["op"] == "delete":
                client.delete(o["event_id"])
                record = {"uid": o["uid"], "op": "deleted", "event_id": o["event_id"]}
            else:
                day = date.fromisoformat(o["day"])
                common = dict(
                    subject=o["subject"],
                    day=day,
                    body=o["body"],
                    reminder_minutes=o["reminder"],
                    time_zone=tz,
                )
                if o["op"] == "create":
                    tid = hashlib.sha256(f"{ctx.tenant_slug}:{o['uid']}".encode()).hexdigest()[:32]
                    eid = client.create_all_day(transaction_id=tid, **common)
                else:
                    eid = o["event_id"]
                    client.update_all_day(eid, **common)
                record = {
                    "uid": o["uid"],
                    "op": "created" if o["op"] == "create" else "updated",
                    "event_id": eid,
                    "fp": o["fp"],
                    "subject": o["subject"],
                }
        except GraphMailError as exc:
            anomalies.append(
                Anomaly(code="deadlines.calendar_write_failed", detail=f"{o['uid']}: {exc}")
            )
            continue
        events.append(
            EventSpec(key=f"cal:{o['uid']}:{record['op']}", event_type=CAL_EVENT, payload=record)
        )
    decided = []
    if granted is not None:
        agent, verdict = granted
        decided = [_plan_card(ops, h, provider, decided_by=agent, decided_reason=verdict.reason)]
    return JobOutput(
        status="ok",
        summary=f"deadlines calendar: wrote {len(events)} of {len(ops)} change(s) to {provider}",
        events=events,
        anomalies=anomalies,
        approvals=decided,
    )


def _plan_card(ops: list[dict], h: str, provider: str, **decided: str) -> ApprovalSpec:
    titles = [f"{o['op']}: {o.get('subject', '')} {o.get('day', '')}".strip() for o in ops[:12]]
    return ApprovalSpec(
        key=f"calendar:{h}",
        action_type=CAL_ACTION,
        params={"plan": h, "provider": provider, "changes": titles},
        reason=f"write deadlines to your calendar ({_counts(ops)})",
        **decided,
    )
