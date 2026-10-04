"""Deadlines agent jobs: scan and done.

``scan`` reads the tenant's obligations file, works out each obligation's open
occurrences, records one ``deadlines.reminder`` event per lead window as it is
crossed (once ever, at the tightest window crossed, and once more when it goes
overdue), and rewrites the calendar file. ``done`` records the owner's act, so
a one-off closes and a recurring obligation rolls to its next date.

Pure code (schema.py has the boundary classification): nothing here calls a
model, sends anything, or moves money. The weekly brief reads the events.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from ...engine.clock import local_today
from ...engine.config import tenant_dir
from ...engine.contracts import EventSpec, JobContext, JobHandler, JobOutput
from ...engine.runkey import RunKey
from .ics import CalendarEntry, render_calendar
from .schema import Obligation, load_obligations, nth_occurrence

REMINDER_EVENT = "deadlines.reminder"
DONE_EVENT = "deadlines.done"
MAX_OPEN_PER_OBLIGATION = 24
"""A recurring obligation whose anchor sits years back with nothing marked
done would list every missed occurrence; the newest 24 are plenty to act on."""


# ---- inputs -------------------------------------------------------------------


def _obligations_path(ctx: JobContext) -> Path:
    override = ctx.params.get("obligations_file") or ctx.tenant.deadlines.file
    return Path(override) if override else tenant_dir(ctx.tenant_slug) / "obligations.toml"


def _ics_path(ctx: JobContext) -> Path | None:
    configured = ctx.params.get("ics_path") or ctx.tenant.deadlines.ics_path
    return Path(configured) if configured else None


def _today(ctx: JobContext) -> date:
    given = ctx.params.get("today")
    return date.fromisoformat(given or local_today(ctx.tenant.identity.timezone))


def _ledger_state(ctx: JobContext) -> tuple[set[tuple[str, str]], set[str]]:
    """(done occurrences as (id, due), reminder fire keys already recorded)."""
    done: set[tuple[str, str]] = set()
    fired: set[str] = set()
    for e in ctx.ledger.read_event_log():
        payload = e.get("payload") or {}
        if e.get("event_type") == DONE_EVENT:
            done.add((str(payload.get("id")), str(payload.get("due"))))
        elif e.get("event_type") == REMINDER_EVENT:
            fired.add(str(payload.get("fire_key")))
    return done, fired


def _key(ctx: JobContext, job: str) -> RunKey:
    key = RunKey(ctx, f"deadlines-{job}")
    key.param("obligations_file")
    key.param("ics_path")
    key.param("today")
    key.config("deadlines", "identity.timezone", "identity.legal_name")
    path = _obligations_path(ctx)
    key.add("obligations", path.read_bytes().hex() if path.is_file() else "absent")
    key.value("today", _today(ctx).isoformat())
    done, fired = _ledger_state(ctx)
    key.value("done", sorted(done))
    key.value("fired", sorted(fired))
    return key


# ---- the arithmetic -------------------------------------------------------------


def open_occurrences(
    ob: Obligation, *, today: date, leads: list[int], done: set[tuple[str, str]]
) -> list[date]:
    """Every not-done occurrence up to today's farthest lead window, plus the
    first not-done one beyond it (so the calendar always shows what is next).
    A one-off has at most one."""
    horizon = today + timedelta(days=max(leads, default=0))
    is_done = lambda d: (ob.id, d.isoformat()) in done  # noqa: E731
    if not ob.every:
        return [] if is_done(ob.due) else [ob.due]
    found: list[date] = []
    n = 0
    while True:
        d = nth_occurrence(ob.due, ob.every, n)
        n += 1
        if is_done(d):
            continue
        found.append(d)
        if d > horizon:
            break
    return found[-MAX_OPEN_PER_OBLIGATION:]


def tier_for(days_left: int, leads: list[int]) -> str | None:
    """The tightest window crossed, ``overdue`` once the date has passed, or
    ``None`` while it is still outside every window."""
    if days_left < 0:
        return "overdue"
    crossed = [lead for lead in leads if days_left <= lead]
    return str(min(crossed)) if crossed else None


def _label(ob: Obligation) -> str:
    return f"{ob.title} ({ob.who})" if ob.who else ob.title


# ---- scan -------------------------------------------------------------------------


def _scan_key(ctx: JobContext) -> str:
    return _key(ctx, "scan").digest()


def _scan_run(ctx: JobContext) -> JobOutput:
    path = _obligations_path(ctx)
    if not path.is_file():
        return JobOutput(status="ok", summary=f"deadlines: no obligations file ({path})")
    obligations = load_obligations(path)
    today = _today(ctx)
    done, fired = _ledger_state(ctx)
    default_leads = ctx.tenant.deadlines.lead_days

    events: list[EventSpec] = []
    actions: list[str] = []
    entries: list[CalendarEntry] = []
    overdue = 0
    for ob in obligations:
        leads = ob.leads(default_leads)
        horizon = today + timedelta(days=max(leads, default=0))
        for due in open_occurrences(ob, today=today, leads=leads, done=done):
            days_left = (due - today).days
            entries.append(
                CalendarEntry(
                    uid=f"{ob.id}-{due:%Y%m%d}@{ctx.tenant_slug}",
                    due=due,
                    summary=_label(ob),
                    description="\n".join(
                        part
                        for part in (
                            ob.kind and f"Kind: {ob.kind}",
                            ob.notes,
                            ob.evidence and f"Evidence: {ob.evidence}",
                        )
                        if part
                    ),
                    leads=tuple(leads),
                )
            )
            if due > horizon:
                continue
            tier = tier_for(days_left, leads)
            if tier is None:
                continue
            overdue += tier == "overdue"
            when = f"{-days_left} days overdue" if days_left < 0 else f"{days_left} days"
            actions.append(f"{_label(ob)}: due {due.isoformat()}, {when}")
            fire_key = f"{ob.id}:{due.isoformat()}:{tier}"
            if fire_key in fired or ctx.shadow:
                continue
            fired.add(fire_key)
            events.append(
                EventSpec(
                    key=f"reminder:{fire_key}",
                    event_type=REMINDER_EVENT,
                    payload={
                        "fire_key": fire_key,
                        "id": ob.id,
                        "title": ob.title,
                        "who": ob.who,
                        "kind": ob.kind,
                        "due": due.isoformat(),
                        "days_left": days_left,
                        "tier": tier,
                        "notes": ob.notes,
                        "evidence": ob.evidence,
                    },
                )
            )

    ics = _ics_path(ctx)
    if ics is not None and not ctx.shadow:
        ctx.guard.check_write(ics)
        ics.parent.mkdir(parents=True, exist_ok=True)
        tmp = ics.with_name(f".{ics.name}.tmp")
        tmp.write_text(
            render_calendar(
                entries, name=f"{ctx.tenant.identity.legal_name} deadlines", stamp=today
            ),
            encoding="utf-8",
            newline="",
        )
        os.replace(tmp, ics)
        actions.append(f"calendar written: {ics.name} ({len(entries)} events)")

    summary = (
        f"deadlines {today.isoformat()}: {len(entries)} open, "
        f"{len(events)} new reminder(s), {overdue} overdue"
    )
    listed = [a for a in actions if not a.startswith("calendar written")]
    if listed:
        summary += "; " + "; ".join(listed)
    return JobOutput(status="ok", summary=summary, actions=actions, events=events)


# ---- done ---------------------------------------------------------------------------


def _done_key(ctx: JobContext) -> str:
    key = _key(ctx, "done")
    key.param("id")
    key.param("due")
    return key.digest()


def _done_run(ctx: JobContext) -> JobOutput:
    """``--param id=<id>`` (and optionally ``due=YYYY-MM-DD``; default the
    earliest open occurrence): the owner did the thing."""
    ob_id = str(ctx.params.get("id") or "")
    obligations = {ob.id: ob for ob in load_obligations(_obligations_path(ctx))}
    if ob_id not in obligations:
        raise ValueError(
            f"no obligation {ob_id!r}; ids: {', '.join(sorted(obligations)) or 'none'}"
        )
    ob = obligations[ob_id]
    done, _ = _ledger_state(ctx)
    given = ctx.params.get("due")
    if given:
        due = date.fromisoformat(str(given))
        n = 0
        while (d := nth_occurrence(ob.due, ob.every, n)) < due and ob.every:
            n += 1
        if d != due:
            raise ValueError(f"{due.isoformat()} is not a due date of {ob_id!r}")
    else:
        opens = open_occurrences(ob, today=_today(ctx), leads=[0], done=done)
        if not opens:
            return JobOutput(status="ok", summary=f"deadlines: {ob_id} is already done")
        due = opens[0]
    if (ob_id, due.isoformat()) in done:
        return JobOutput(
            status="ok", summary=f"deadlines: {ob_id} due {due.isoformat()} is already done"
        )
    if ctx.shadow:
        return JobOutput(
            status="ok", summary=f"deadlines: would mark {ob_id} due {due.isoformat()} done"
        )
    return JobOutput(
        status="ok",
        summary=f"deadlines: {_label(ob)} due {due.isoformat()} marked done",
        events=[
            EventSpec(
                key=f"done:{ob_id}:{due.isoformat()}",
                event_type=DONE_EVENT,
                payload={"id": ob_id, "due": due.isoformat(), "on": _today(ctx).isoformat()},
            )
        ],
    )


def _calendar_key(ctx: JobContext) -> str:
    from .calendar_sync import calendar_key

    return calendar_key(ctx)


def _calendar_run(ctx: JobContext) -> JobOutput:
    from .calendar_sync import calendar_run

    return calendar_run(ctx)


JOBS: dict[str, JobHandler] = {
    "scan": JobHandler(key=_scan_key, run=_scan_run),
    "done": JobHandler(key=_done_key, run=_done_run),
    "calendar": JobHandler(key=_calendar_key, run=_calendar_run),
}
