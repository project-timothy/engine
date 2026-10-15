"""Where each receipt stands (the structure review's proposal 1, decided
2026-10-14): one record per receipt with a life of its own, read from what
the books already hold. Events stay its history; nothing here writes.

A record moves through captured, read, said, placed, approved, reported,
sent and paid back. A yes is to the receipts a card named, by their
fingerprints, so a late receipt never reopens a month already approved:
it waits on its own small card. A yes holds only while the receipt's facts
hold; a new reading, amount, price or category after the yes puts the
receipt back in front of its reviewer, since only approved values enter
the books (invariant 2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .schema import parse_amount_tag, parse_foreign_claim

LANDED_EVENT = "expense.receipt_landed"
PROPOSED_EVENT = "expense.extract_proposed"
REFERENCE_EVENT = "expense.reference_detected"
PRICED_EVENT = "expense.owner_priced"  # an owner-supplied amount for one receipt
CATEGORY_EVENT = "expense.category_set"  # a receipt placed in a category, and who said so
AMOUNT_EVENT = "expense.amount_said"  # a door receipt's amount, in the person's own words
READ_EVENT = "expense.amount_read"  # what its photo reads: a proposal, never a price
DATED_EVENT = "expense.receipt_dated"  # the receipt's own day, when it was sent later
SENT_EVENT = "institution.claim.sent"
DRIVE_EVENT = "mileage.drive"  # a drive from the phone: whose, when, how far, never where
PURPOSE_EVENT = "mileage.purpose"  # what a drive was for, in the person's own words
LINE_EVENT = "bank.line"  # one statement line kept: the ministry's, or one the person picked
TRANSFER_EVENT = "bank.transfer_matched"  # a ministry line that landed in the person's account
SCHOOL_EVENT = "school.payment"  # a scholarship or school fee from the person's list
LINE_PURPOSE_EVENT = "bank.line_purpose"  # what a line was for, in the person's own words

REVIEW_CARD = "expenses.report_review"
SEND_CARD = "expenses.institution_send"  # the person's yes to send receipts to their institution

STATUS_OPEN = "Open"
STATUS_RECORDED = "Reimbursed-Recorded"
STATUS_REIMBURSED = "Reimbursed"

STAGES = ("captured", "read", "said", "placed", "approved", "reported", "sent", "paid back")

FACT_EVENTS = (
    PROPOSED_EVENT,
    PRICED_EVENT,
    CATEGORY_EVENT,
    AMOUNT_EVENT,
    READ_EVENT,
    PURPOSE_EVENT,
    DATED_EVENT,
    LINE_PURPOSE_EVENT,
)
"""What a reviewer's yes rests on: a newer one of these reopens the receipt."""


@dataclass(frozen=True)
class Yes:
    card: int
    on: str  # when the yes was said (UTC, ISO)


@dataclass(frozen=True)
class Record:
    sha: str
    person: str
    month: str
    file: str
    stage: str
    approved_on: str = ""


def drive_id(payload: dict) -> str:
    """A drive's identity: whose and when it started."""
    return f"{payload.get('person', '')}|{payload.get('start', '')}"


def drive_sha(drive: str) -> str:
    """A drive's fingerprint, where a receipt's is its file's: a report line
    and a card name a drive by it."""
    return hashlib.sha256(f"drive:{drive}".encode()).hexdigest()


def drive_dollars(miles: Decimal, rate: Decimal) -> Decimal:
    """A ministry drive in dollars at a rate per mile, to the cent."""
    return (miles * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


UNDATED = "_receipt_undated"
"""In a catch-up receipt's name until its own day is known (t-catch-up)."""


def days(events: list[dict]) -> dict[str, str]:
    """Each receipt's own day, where one was said or read after it was sent
    (the latest wins)."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"]["day"])
        for e in events
        if e.get("event_type") == DATED_EVENT and e.get("payload", {}).get("sha256")
    }


def dated_name(name: str, day: str) -> str:
    """A door receipt's name on its own day: the day it was sent replaced by
    the day it was paid, so its month and its rate are that day's."""
    return f"{day}{name[10:]}" if day else name


def _sha(e: dict) -> str:
    payload = e.get("payload", {})
    if e.get("event_type") == PURPOSE_EVENT:
        return drive_sha(str(payload.get("drive", "")))
    if e.get("event_type") == DRIVE_EVENT:
        return drive_sha(drive_id(payload))
    return str(payload.get("sha256") or payload.get("receipt") or "")


def card_receipts(params: dict) -> set[str]:
    """The receipts a card names, by their full fingerprints."""
    return {s for s in str(params.get("receipts") or "").split(",") if s}


def yeses(cards: Iterable[dict], events: list[dict], changed: Iterable[str] = ()) -> dict[str, Yes]:
    """Each receipt an approved review card said yes to, and when, while
    nothing it rests on has changed since. ``cards`` are approval rows with
    their params parsed; ``changed`` names receipts this very run is
    changing (a category or price not yet in the log)."""
    out: dict[str, Yes] = {}
    approved = [
        c for c in cards if c.get("status", "approved") == "approved" and card_receipts(c["params"])
    ]
    for card in sorted(approved, key=lambda c: str(c.get("resolved_at") or c.get("created_at"))):
        on = str(card.get("resolved_at") or card.get("created_at") or "")
        for sha in card_receipts(card["params"]):
            out[sha] = Yes(int(card["id"]), on)
    last: dict[str, str] = {}
    for e in events:
        if e.get("event_type") in FACT_EVENTS and (sha := _sha(e)):
            last[sha] = max(last.get(sha, ""), str(e.get("created_at") or ""))
    skip = set(changed)
    return {s: y for s, y in out.items() if s not in skip and last.get(s, "") <= y.on}


def where(events: list[dict], cards: Iterable[dict], lines: Iterable[dict]) -> dict[str, Record]:
    """Every receipt's record: how far along it is. ``cards`` are the
    review and send cards (any status); ``lines`` are report lines with
    the report's status (``receipt_sha256``, ``status``)."""
    cards = list(cards)
    review = [c for c in cards if c.get("action_type", REVIEW_CARD) == REVIEW_CARD]
    sent_yes = set().union(
        *(
            card_receipts(c["params"])
            for c in cards
            if c.get("action_type") == SEND_CARD and c.get("status") == "approved"
        )
    )
    yes = yeses(review, events)
    reached: dict[str, set[str]] = {}
    landed: dict[str, dict] = {}
    gone: set[str] = set()
    for e in events:
        kind, sha = e.get("event_type"), _sha(e)
        if not sha:
            continue
        payload = e.get("payload", {})
        if kind == REFERENCE_EVENT:
            gone.add(sha)
            continue
        first = _landing(kind, payload)
        if first is not None and (kind != LINE_EVENT or sha not in landed):
            landed[sha] = first
        reached.setdefault(sha, set()).update(_stages(kind, payload))
    for sha in set(yes) | sent_yes:
        reached.setdefault(sha, set()).add("approved")
    for line in lines:
        sha = str(line.get("receipt_sha256") or "")
        stage = (
            "paid back"
            if line.get("status") in (STATUS_RECORDED, STATUS_REIMBURSED)
            else "reported"
        )
        reached.setdefault(sha, set()).update({"approved", stage})
    dated = days(events)
    out = {}
    for sha, payload in landed.items():
        if sha in gone:
            continue
        stage = max(reached.get(sha, {"captured"}), key=STAGES.index)
        out[sha] = Record(
            sha=sha,
            person=str(payload.get("person", "")),
            month=dated[sha][:7] if sha in dated else str(payload.get("month", "")),
            file=str(payload.get("file", "")),
            stage=stage,
            approved_on=yes[sha].on if sha in yes else "",
        )
    return out


_STAGE_OF = {
    PURPOSE_EVENT: "said",
    DATED_EVENT: "read",
    READ_EVENT: "read",
    AMOUNT_EVENT: "said",
    PRICED_EVENT: "said",
    SENT_EVENT: "sent",
    LINE_PURPOSE_EVENT: "said",
}


def _landing(kind: str | None, payload: dict) -> dict | None:
    """What a record is, from the event that brings it in: a receipt, a
    drive, a statement line, or a school payment (t-statements); None for
    any other event."""
    if kind == LANDED_EVENT:
        return payload
    if kind == DRIVE_EVENT:
        return {**payload, "month": str(payload.get("start", ""))[:7]}
    if kind in (LINE_EVENT, SCHOOL_EVENT):
        day = str(payload.get("day", ""))
        words = str(payload.get("words") or payload.get("student") or "")
        return {**payload, "month": day[:7], "file": f"{day} {words}"}
    return None


def _stages(kind: str | None, payload: dict) -> set[str]:
    """The stages one event shows a record has reached."""
    if kind in (DRIVE_EVENT, LANDED_EVENT, LINE_EVENT, SCHOOL_EVENT):
        said = (
            (kind == LANDED_EVENT and _said_with_photo(str(payload.get("file", ""))))
            or kind == SCHOOL_EVENT
            or (kind == LINE_EVENT and payload.get("account") == "own")
        )
        return {"captured", "said"} if said else {"captured"}
    if kind == PROPOSED_EVENT:
        return {"read"} if payload.get("amount_cents") else set()
    if kind == CATEGORY_EVENT:
        return {"placed"} if payload.get("category") else set()
    return {_STAGE_OF[kind]} if kind in _STAGE_OF else set()


def _said_with_photo(name: str) -> bool:
    """A door receipt whose amount the person said with the photo carries it
    in its name ("2026-10-12_receipt_NPR4300.00.jpg")."""
    return parse_amount_tag(name) is not None or parse_foreign_claim(name) is not None
