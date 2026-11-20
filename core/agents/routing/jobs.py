"""Routing: the clock behind cards on a route (#436; brief.md).

Only for a tenant with ``authority.toml`` (``ctx.authority``); for any other
tenant every job returns at once and records nothing.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ...authority import UNDESCRIBED, CardRule, Request, card_request, evaluate
from ...authority.routing import LEVELS, Delegation, check_delegation, level_due
from ...engine import clock
from ...engine.authority_gate import load_delegations, waiting
from ...engine.clock import local_today
from ...engine.contracts import EventSpec, JobContext, JobHandler, JobOutput
from ...engine.registry import load_card_authority
from ...engine.runkey import RunKey
from ...tools.viewer import Viewer, sees_every_month
from ..deadlines import form990
from ..deadlines.jobs import DONE_EVENT, obligations_path, open_occurrences
from ..deadlines.schema import ObligationsError, load_obligations
from ..expenses import statements
from ..expenses.records import DRIVE_EVENT, PURPOSE_EVENT, drive_id
from ..expenses.records import REVIEW_CARD as MONTH_CARD  # a person's month, waiting for its yes
from ..policy.jobs import clock_in_force, pay_in_force

REMINDER_EVENT = "route.reminder"
DELEGATED_EVENT = "route.delegated"
REVOKED_EVENT = "route.revoked"
NOTICE_EVENT = "route.notice"  # one email the box sends (doorkeeper send-notices)


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
    now = clock.utc_now().isoformat()
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


# ---- notices: the emails the box sends (#453's delivery half) ---------------


def _noticed(ctx: JobContext) -> set[str]:
    rows = ctx.ledger.conn.execute(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant_slug, NOTICE_EVENT),
    ).fetchall()
    return {str(json.loads(r["payload_json"] or "{}").get("id", "")) for r in rows}


def _slug(name: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in name.casefold()).split())


def _notices_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "routing-notices")
    key.param("today")
    key.config("identity.timezone")
    if ctx.authority is not None:
        key.value("authority", ctx.authority.sha256)
        key.rows(
            "pending",
            (
                (r["id"], json.dumps(r["params"], sort_keys=True))
                for r in ctx.ledger.list_approvals(ctx.tenant_slug, status="pending")
            ),
        )
        key.value("noticed", sorted(_noticed(ctx)))
        key.value("today", _today(ctx).isoformat())
        key.value("unspoken", sorted(_unspoken_this_week(ctx, _today(ctx))))
        cash = _cash_due(ctx, _today(ctx))
        if cash:  # only where someone holds cash: everyone else's key is unchanged
            key.value("cash", sorted(cash))
        key.config("deadlines")  # where the obligations file is
        filings = _filing_due(ctx, _today(ctx))
        if filings:  # only in a 990's last month: everyone else's key is unchanged
            key.value("filings", sorted(filings))
        packs = _packs_due(ctx)
        if packs:  # only once a month closes: everyone else's key is unchanged
            key.value("packs", sorted(packs))
        pays = _pay_due(ctx, _today(ctx))
        if pays:  # only where the board set pay: everyone else's key is unchanged
            key.value("pays", sorted(pays))
    return key.digest()


CHECK_IN_DAY = 4  # Friday


def _unspoken_this_week(ctx: JobContext, today: date) -> set[str]:
    """Whose drives of the last seven days nobody has said what they were
    for: a person's id, as authority.toml names them."""
    log = ctx.ledger.read_event_log()
    spoken = {e["payload"].get("drive") for e in log if e.get("event_type") == PURPOSE_EVENT}
    since = (today - timedelta(days=6)).isoformat()
    return {
        _slug(str(e["payload"].get("person", "")))
        for e in log
        if e.get("event_type") == DRIVE_EVENT
        and since <= str(e["payload"].get("start", ""))[:10] <= today.isoformat()
        and drive_id(e["payload"]) not in spoken
    }


FILING_NOTICE_DAYS = 30
"""A 990 is in the morning email from 30 days out: mid-April for May 15."""


def _filing_due(ctx: JobContext, today: date) -> set[tuple[str, str]]:
    """Whoever runs the ministry's money, told once that its 990 is due
    within a month (t-990n), as (notice id, the due month). Not once it is
    marked done."""
    path = obligations_path(ctx)
    if ctx.authority is None or not path.is_file():
        return set()
    try:
        obligations = [ob for ob in load_obligations(path) if ob.id == form990.OBLIGATION_ID]
    except ObligationsError:
        return set()  # the deadlines job names a broken file; a notice never guesses
    if not obligations:
        return set()
    done = {
        (str(e["payload"].get("id")), str(e["payload"].get("due")))
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == DONE_EVENT
    }
    policy = ctx.authority.policy
    money = [
        p
        for p, who in policy.people.items()
        if who.kind == "person" and policy.holds_any_scope(p, "release", "payment")
    ]
    out = set()
    for ob in obligations:
        for due in open_occurrences(ob, today=today, leads=[FILING_NOTICE_DAYS], done=done):
            if 0 <= (due - today).days <= FILING_NOTICE_DAYS:
                out |= {(f"filing:{ob.id}:{due.isoformat()}:{p}", f"{due:%Y-%m}") for p in money}
    return out


def _pay_due(ctx: JobContext, today: date) -> set[tuple[str, str, str]]:
    """Whoever pays the ministry's bills, told on a missionary's pay day that
    their pay is due (c34), once a month, as (notice id, whose pay by their
    id, the month). Tim never sends the money; the email names no amount."""
    if ctx.authority is None or ctx.authority.rules is None:
        return set()
    policy, month = ctx.authority.policy, f"{today:%Y-%m}"
    money = [
        p
        for p, who in policy.people.items()
        if who.kind == "person" and policy.holds_any_scope(p, "release", "payment")
    ]
    out = set()
    for person in policy.people:
        pay = pay_in_force(ctx.ledger.conn, ctx.tenant_slug, ctx.authority.rules, person, month)
        if pay is None or today.day < pay.day:
            continue
        out |= {(f"pay:{person}:{month}:{p}", person, month) for p in money}
    return out


def _cash_due(ctx: JobContext, today: date) -> set[str]:
    """Who should count their cash today, as notice ids (t-cash-count,
    t-advance-clock): on the 1st, anyone holding cash; any day, anyone whose
    oldest cash is 45 days old with no count since it was drawn, once for
    that cash."""
    log = ctx.ledger.read_event_log()
    day = today.isoformat()
    _drawn, held = statements.cash(log, through=day)
    if not held:
        return set()
    counted: dict[str, str] = {}
    for c in statements.counts(log):
        counted[_slug(str(c["person"]))] = max(counted.get(_slug(str(c["person"])), ""), c["day"])
    out = set()
    for person, _currency in held:
        who = _slug(person)
        if today.day == 1:
            out.add(f"cash:{today:%Y-%m}:{who}")
    authority = ctx.authority
    rules = authority.rules if authority is not None and not authority.rules_error else None
    policy = authority.policy if authority is not None else None
    for (person, _currency), (drawn, _left) in statements.cash_age(log, day).items():
        who = _slug(person)
        nudge = clock_in_force(ctx.ledger.conn, ctx.tenant_slug, rules, policy, who).nudge
        if statements.days_old(drawn, day) >= nudge and counted.get(who, "") < drawn:
            out.add(f"cashage:{drawn}:{who}")
    return out


def _packs_due(ctx: JobContext) -> set[tuple[str, str, str]]:
    """Who is mailed a month's board pack (t-pack-by-email, the owner
    2026-11-12), as (notice id, person, month): once the newest month with
    month cards has closed, each person's newest card for it approved, everyone
    who looks over every month, once. Only the newest month, so a box that
    turns this on never mails a backlog."""
    if ctx.authority is None:
        return set()
    newest: dict[tuple[str, str], dict] = {}  # (month, person) -> their newest card
    for row in ctx.ledger.list_approvals(ctx.tenant_slug):
        if row["action_type"] == MONTH_CARD and row["params"].get("month"):
            newest[(str(row["params"]["month"]), str(row["params"].get("person", "")))] = row
    if not newest:
        return set()
    month = max(m for m, _p in newest)
    if any(row["status"] != "approved" for (m, _p), row in newest.items() if m == month):
        return set()
    policy = ctx.authority.policy
    return {
        (f"pack:{month}:{p}", p, month)
        for p, who in policy.people.items()
        if who.kind == "person" and sees_every_month(Viewer(policy, p))
    }


def _notices_run(ctx: JobContext) -> JobOutput:
    """The emails the box sends, decided here and worded by the doorkeeper
    (2026-10-13): a reviewer hears once that a person's month is ready,
    and so does whoever approves it, whose email carries the month's link
    and stands in for that day's "waiting" (the owner, 2026-10-14); anyone a
    card waits on hears at most once a day that a yes is waiting,
    and on Fridays someone whose drives this week nobody has spoken for is
    asked, once, to tell Tim which were for the ministry. Someone holding
    ATM cash is asked to count it on the 1st, and once when their oldest
    cash turns 45 days with no count since (2026-10-15). Whoever runs the
    ministry's money hears once, a month ahead, that its 990 is due (t-990n).
    Once a month closes, whoever looks over every month is mailed its board
    pack, once (t-pack-by-email). A notice names people and a month, never a
    figure or what a card is: a
    leader's "waiting" names whose things wait for them (chunk 12, the
    morning brief by role), and ``more`` says something else waits too, one
    of their own or about no one."""
    if ctx.authority is None:
        return JobOutput(status="ok", summary="routing: no authority.toml, no notices")
    policy = ctx.authority.policy
    today = _today(ctx)
    noticed = _noticed(ctx)
    now = clock.utc_now().isoformat()
    notices: list[dict] = []
    waits: dict[str, set[int]] = {}  # person -> the cards waiting on them
    whose: dict[str, set[str]] = {}  # person -> the people those cards are about
    more: set[str] = set()  # people with a card waiting about no one else
    told: dict[str, set[int]] = {}  # person -> month cards whose email goes today
    for item in waiting(policy, ctx.ledger, ctx.tenant_slug, today=today, now=now):
        rules = load_card_authority(_agent_of(ctx, item["card"]))
        rule = rules.get(str(item["action_type"]), UNDESCRIBED)
        deciders = [
            p
            for p in item["waiting_on"]
            if evaluate(policy, card_request(rule, p, item["params"], policy)).allowed
        ]
        about = _slug(str(item["params"].get("person", "")))
        for p in deciders:
            waits.setdefault(p, set()).add(int(item["card"]))
            if about and about != p and about in policy.people:
                whose.setdefault(p, set()).add(about)
            else:
                more.add(p)
        if item["action_type"] != MONTH_CARD:
            continue
        month = str(item["params"].get("month", ""))
        for p, who in policy.people.items():
            if who.kind != "person" or p == about:
                continue
            review = Request(p, "review", "expense.report", scope=about)
            if p not in deciders and not evaluate(policy, review).allowed:
                continue
            notice_id = f"month:{item['card']}:{p}"
            notices.append(
                {"id": notice_id, "to": p, "kind": "month_ready", "about": about, "month": month}
            )
            if notice_id not in noticed:
                told.setdefault(p, set()).add(int(item["card"]))
    if today.weekday() == CHECK_IN_DAY:
        for p in sorted(_unspoken_this_week(ctx, today)):
            if p in policy.people and policy.people[p].kind == "person":
                notices.append(
                    {
                        "id": f"drives:{today.isoformat()}:{p}",
                        "to": p,
                        "kind": "drives_week",
                        "about": "",
                        "month": "",
                    }
                )
    for notice_id in sorted(_cash_due(ctx, today)):
        p = notice_id.rsplit(":", 1)[1]
        if p in policy.people and policy.people[p].kind == "person":
            notices.append(
                {"id": notice_id, "to": p, "kind": "cash_count", "about": "", "month": ""}
            )
    for notice_id, month in sorted(_filing_due(ctx, today)):
        notices.append(
            {
                "id": notice_id,
                "to": notice_id.rsplit(":", 1)[1],
                "kind": "filing_990",
                "about": "",
                "month": month,
            }
        )
    notices.extend(
        {"id": notice_id, "to": p, "kind": "board_pack", "about": "", "month": month}
        for notice_id, p, month in sorted(_packs_due(ctx))
    )
    notices.extend(
        {
            "id": notice_id,
            "to": notice_id.rsplit(":", 1)[1],
            "kind": "pay_due",
            "about": person,
            "month": month,
        }
        for notice_id, person, month in sorted(_pay_due(ctx, today))
    )
    for p in sorted(waits):
        if waits[p] <= told.get(p, set()):
            continue  # the month's own email, with its link, says it today
        notices.append(
            {
                "id": f"waiting:{today.isoformat()}:{p}",
                "to": p,
                "kind": "waiting",
                "about": "",
                "month": "",
                "people": sorted(whose.get(p, set())),
                "more": p in more,
            }
        )
    events = [
        EventSpec(
            key=f"notice:{n['id']}",
            event_type=NOTICE_EVENT,
            payload={**n, "day": today.isoformat()},
        )
        for n in notices
        if n["id"] not in noticed
    ]
    return JobOutput(
        status="ok", summary=f"routing: {len(events)} notice(s) for the box to send", events=events
    )


def _agent_of(ctx: JobContext, card: int) -> str:
    row = ctx.ledger.conn.execute(
        "SELECT agent FROM approval_queue WHERE id = ?", (card,)
    ).fetchone()
    return str(row["agent"]) if row else ""


def notices_since(events: list[dict], day: str) -> list[dict]:
    """The notices recorded on or after ``day``, for ``engine notices``."""
    return [
        {
            k: e["payload"][k]
            for k in ("id", "to", "kind", "about", "month", "people", "more")
            if k in e["payload"]
        }
        for e in events
        if e.get("event_type") == NOTICE_EVENT and str(e["payload"].get("day", "")) >= day
    ]


JOBS: dict[str, JobHandler] = {
    "tick": JobHandler(key=_tick_key, run=_tick_run),
    "notices": JobHandler(key=_notices_key, run=_notices_run),
    "delegate": JobHandler(key=_delegation_key, run=_delegate_run),
    "revoke": JobHandler(key=_delegation_key, run=_revoke_run),
}

# Routing raises no cards (#435 declares what each card is).
CARD_AUTHORITY: dict[str, CardRule] = {}
