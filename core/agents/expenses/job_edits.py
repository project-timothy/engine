"""Small edits a person makes by saying so: a receipt's amount and date, a
drive, and a drive's purpose.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from ...engine.contracts import EventSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from .common import _events_of, _payload_sha, _persons
from .records import (
    AMOUNT_EVENT,
    DATED_EVENT,
    DRIVE_EVENT,
    PURPOSE_EVENT,
    READ_EVENT,
    drive_id,
)
from .report_lines import _TAG, _is_door_receipt, _rate_once_known, _unconsumed

# ---- job: amount ------------------------------------------------------------


def _amount_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expamount")
    sha = str(key.param("sha"))
    for name in ("tag", "said", "by", "read", "payee", "category", "filing_dir"):
        key.param(name)
    key.add(
        "before",
        sum(
            1
            for e in ctx.ledger.read_event_log()
            if e.get("event_type") in (AMOUNT_EVENT, READ_EVENT)
            and e.get("payload", {}).get("sha256") == sha
        ),
    )
    key.config("expenses")
    return key.digest()


def _amount_run(ctx: JobContext) -> JobOutput:
    """A door receipt's amount after its photo (board item t-receipt-photo,
    2026-10-13): what its photo reads (``read=photo``, a proposal) or what
    the person said (their words, or their yes to the reading). The door
    reads the words with code (core/tools/receipts.claim_in) and passes the
    claim as a tag; nothing here prices anything, the month does that from
    the tag the way it prices a claim said with the photo."""
    sha = str(ctx.params.get("sha") or "")
    tag = str(ctx.params.get("tag") or "")
    if not _TAG.match(tag):
        return JobOutput(status="ok", summary=f"expenses amount: {tag!r} is not an amount")
    receipt = next((i for i in _unconsumed(ctx) if i["sha256"] == sha), None)
    if receipt is None or not _is_door_receipt(str(receipt.get("file", ""))):
        return JobOutput(status="ok", summary="expenses amount: no such door receipt waiting")
    read = ctx.params.get("read") == "photo"
    kind = READ_EVENT if read else AMOUNT_EVENT
    before = sum(
        1
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == kind and e.get("payload", {}).get("sha256") == sha
    )
    payload = {"sha256": sha, "tag": tag}
    if read:  # who it reads as paid, and for what: proposals beside the amount
        payload |= {k: str(ctx.params[k]) for k in ("payee", "category") if ctx.params.get(k)}
    else:
        payload |= {
            "by": str(ctx.params.get("by") or ""),
            "said": str(ctx.params.get("said") or ""),
        }
    amount = EventSpec(key=f"expamount:{kind}:{sha}:{before}", event_type=kind, payload=payload)
    return JobOutput(
        status="ok",
        summary=f"expenses amount: {receipt.get('file')} "
        + (
            f"reads {tag} (a proposal)"
            if read
            else f"is {tag} (said by {payload['by'] or 'nobody named'})"
        ),
        events=[amount, *_rate_once_known(ctx, amount)],
    )


# ---- job: date --------------------------------------------------------------


def _date_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expdate")
    sha = str(key.param("sha"))
    for name in ("day", "said", "by", "read"):
        key.param(name)
    key.add("before", sum(1 for e in _events_of(ctx, DATED_EVENT) if _payload_sha(e) == sha))
    key.config("expenses")
    return key.digest()


def _date_run(ctx: JobContext) -> JobOutput:
    """A receipt's own day, for one sent later (t-catch-up, 2026-10-14): what
    the person said, or what its photo reads (``read=photo``). It files the
    receipt in that day's month and prices it at that day's rate; nothing
    here prices anything."""
    sha = str(ctx.params.get("sha") or "")
    day = str(ctx.params.get("day") or "")
    try:
        date.fromisoformat(day)
    except ValueError:
        return JobOutput(status="ok", summary=f"expenses date: {day!r} is not a day")
    if len(day) != 10 or not any(i["sha256"] == sha for i in _unconsumed(ctx)):
        return JobOutput(status="ok", summary="expenses date: no such receipt waiting")
    before = sum(1 for e in _events_of(ctx, DATED_EVENT) if _payload_sha(e) == sha)
    payload = {
        "sha256": sha,
        "day": day,
        "said": str(ctx.params.get("said") or ""),
        "by": str(ctx.params.get("by") or ""),
        "read": str(ctx.params.get("read") or ""),
    }
    dated = EventSpec(key=f"expdate:{sha}:{before}", event_type=DATED_EVENT, payload=payload)
    return JobOutput(
        status="ok",
        summary=f"expenses date: {sha[:12]} is from {day}",
        events=[dated, *_rate_once_known(ctx, dated)],
    )


# ---- job: drive -------------------------------------------------------------


def _drive_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expdrive")
    for name in ("person", "start", "end", "miles"):
        key.param(name)
    key.config("expenses")
    return key.digest()


def _drive_run(ctx: JobContext) -> JobOutput:
    """One drive the box's tracker ended (core/mileage; Robb's idea 2,
    2026-10-13): the person as the expense tree names them, when it started
    and ended, and its miles. Nothing about where; what it was for comes
    from the person's own words in the weekly check-in."""
    who = str(ctx.params.get("person") or "")
    name = next(
        (
            p.name
            for p in _persons(ctx)
            if re.sub(r"[^a-z0-9]+", "-", p.name.casefold()).strip("-") == who
        ),
        "",
    )
    try:
        miles = Decimal(str(ctx.params.get("miles")))
        start = datetime.fromisoformat(str(ctx.params.get("start")))
        end = datetime.fromisoformat(str(ctx.params.get("end")))
    except (ArithmeticError, ValueError):
        return JobOutput(status="ok", summary="expenses drive: not a drive")
    if not name or miles <= 0 or end < start:
        return JobOutput(status="ok", summary=f"expenses drive: nobody files expenses as {who!r}")
    payload = {
        "person": name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "miles": str(miles),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses drive: {name}, {miles} miles",
        events=[
            EventSpec(
                key=f"drive:{who}:{payload['start']}", event_type=DRIVE_EVENT, payload=payload
            )
        ],
    )


_PERSONAL = ("personal", "private", "not ministry", "not for the ministry", "my own")


def is_personal(said: str) -> bool:
    """Words that say a drive wasn't the ministry's."""
    text = said.casefold()
    return any(word in text for word in _PERSONAL)


def _purpose_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "exppurpose")
    drive = str(key.param("drive"))
    for name in ("said", "by"):
        key.param(name)
    key.add(
        "before",
        sum(1 for e in _events_of(ctx, PURPOSE_EVENT) if e["payload"].get("drive") == drive),
    )
    key.config("expenses")
    return key.digest()


def _purpose_run(ctx: JobContext) -> JobOutput:
    """What one drive was for, as the person said it (t-mileage part 2): a
    purpose, or that it was personal. The latest word wins. Code reads the
    words; nothing here prices anything."""
    drive = str(ctx.params.get("drive") or "")
    said = str(ctx.params.get("said") or "").strip()
    if not said or not any(drive_id(e["payload"]) == drive for e in _events_of(ctx, DRIVE_EVENT)):
        return JobOutput(status="ok", summary="expenses drive purpose: no such drive")
    before = sum(1 for e in _events_of(ctx, PURPOSE_EVENT) if e["payload"].get("drive") == drive)
    payload = {
        "drive": drive,
        "ministry": not is_personal(said),
        "said": said,
        "by": str(ctx.params.get("by") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses drive purpose: {'ministry' if payload['ministry'] else 'personal'}",
        events=[
            EventSpec(key=f"purpose:{drive}:{before}", event_type=PURPOSE_EVENT, payload=payload)
        ],
    )
