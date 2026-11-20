"""The jobs over a person's statement lines and cash: lines kept, the school
list, a line's purpose and split, cash counted or spent, and the year's budget.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from ...engine.contracts import EventSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from . import balances, fx, processors, statements, year_close
from . import budget as budgets
from . import categories as cats
from .common import _events_of, _payload_sha, _persons
from .records import (
    AMOUNT_EVENT,
    CASH_COUNT_EVENT,
    CASH_USED_EVENT,
    LINE_EVENT,
    LINE_PURPOSE_EVENT,
    LINE_SPLIT_EVENT,
    RECEIPT_SPLIT_EVENT,
    RODE_EVENT,
    SCHOOL_EVENT,
    TRANSFER_EVENT,
)
from .report_lines import _TAG, _rates_for

# ---- jobs: statement, school, line purpose (t-statements) -----------------


def _filer(ctx: JobContext, name: str) -> str:
    """The person as the expense tree names them, or ""."""
    return next((p.name for p in _persons(ctx) if p.name == name), "")


def _statement_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expstatement")
    for name in ("person", "account", "lines", "transfers", "by", "said"):
        key.param(name)
    closed = sorted(year_close.read(ctx.ledger.conn, ctx.tenant_slug))
    if closed:  # a ministry that never closed a year keeps the key it had
        key.value("closed", closed)
    key.config("expenses")
    return key.digest()


def _statement_run(ctx: JobContext) -> JobOutput:
    """Statement lines kept (t-statements, 2026-10-14): every line of the
    ministry's account, or the lines the person picked from their own; and
    the ministry's lines that landed in the person's own account (paid
    back, never spending). The door parsed the file and keeps no copy; a
    line's fingerprint is its event key, so a statement sent twice is the
    same lines."""
    person = _filer(ctx, str(ctx.params.get("person") or ""))
    account = str(ctx.params.get("account") or "")
    try:
        rows = json.loads(str(ctx.params.get("lines") or "[]"))
        moved = json.loads(str(ctx.params.get("transfers") or "[]"))
        lines = [(statements.Line.of(r), str(r.get("looks", ""))) for r in rows]
    except (ValueError, KeyError, TypeError):
        return JobOutput(status="ok", summary="expenses statement: not statement lines")
    if not person or account not in statements.ACCOUNTS:
        return JobOutput(status="ok", summary="expenses statement: nobody's account")
    by, said = str(ctx.params.get("by") or ""), str(ctx.params.get("said") or "")
    closed = year_close.read(ctx.ledger.conn, ctx.tenant_slug)
    left_out = sorted({ln.day[:4] for ln, _l in lines if year_close.closed_for(ln.day, closed)})
    lines = [(ln, looks) for ln, looks in lines if not year_close.closed_for(ln.day, closed)]
    events = []
    # Keys live under each run's key, so a statement sent again (now with its
    # balances, or overlapping the last one) writes only what is new.
    known = {_payload_sha(e) for e in _events_of(ctx, LINE_EVENT)}
    for line, looks in lines:
        if line.sha(person, account) in known:
            continue
        payload = line.payload(person, account) | {"by": by}
        if account == statements.OWN:
            payload |= {"looks": looks, "said": said}
        events.append(
            EventSpec(key=f"bank:{payload['sha256']}", event_type=LINE_EVENT, payload=payload)
        )
    if account == statements.MINISTRY:  # their own account's balance is never kept
        events += _balances(ctx, person, rows, closed, by)
    kept = {e.payload["sha256"]: e.payload for e in events if e.event_type == LINE_EVENT}
    for e in _events_of(ctx, LINE_EVENT):
        kept.setdefault(str(e["payload"].get("sha256")), e["payload"])
    for sha in moved:
        line = kept.get(str(sha))
        if line is None or line.get("account") != statements.MINISTRY:
            continue
        events.append(
            EventSpec(
                key=f"bank-transfer:{sha}",
                event_type=TRANSFER_EVENT,
                payload={"sha256": str(sha), "person": person, "day": line["day"]},
            )
        )
    return JobOutput(
        status="ok",
        summary=f"expenses statement: {len(lines)} line(s) of {person}'s {account} account kept"
        + (f"; the closed {', '.join(left_out)} books' lines left out" if left_out else ""),
        events=events,
    )


def _processor_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expprocessor")
    for name in ("person", "service", "payments", "by"):
        key.param(name)
    closed = sorted(year_close.read(ctx.ledger.conn, ctx.tenant_slug))
    if closed:
        key.value("closed", closed)
    key.config("expenses")
    return key.digest()


def _processor_run(ctx: JobContext) -> JobOutput:
    """A payment service's export kept (c32, processors.py): every gift,
    refund, and payout of the ministry's own account at the service, each
    as an event keyed by its fingerprint, so an export sent twice is the
    same rows. The door parsed the file and keeps no copy."""
    person = _filer(ctx, str(ctx.params.get("person") or ""))
    service = " ".join(str(ctx.params.get("service") or "").split())
    try:
        rows = json.loads(str(ctx.params.get("payments") or "[]"))
        payments = [processors.Payment.of(r) for r in rows]
    except (ValueError, KeyError, TypeError):
        return JobOutput(status="ok", summary="expenses processor: not a service's rows")
    if not person or not service:
        return JobOutput(status="ok", summary="expenses processor: nobody's service")
    by = str(ctx.params.get("by") or "")
    closed = year_close.read(ctx.ledger.conn, ctx.tenant_slug)
    left_out = sorted({p.day[:4] for p in payments if year_close.closed_for(p.day, closed)})
    payments = [p for p in payments if not year_close.closed_for(p.day, closed)]
    known = {_payload_sha(e) for e in _events_of(ctx, processors.PAYMENT_EVENT)}
    events = []
    for pay in payments:
        payload = pay.payload(person, service) | {"by": by}
        if payload["sha256"] in known:
            continue
        events.append(
            EventSpec(
                key=f"processor:{payload['sha256']}",
                event_type=processors.PAYMENT_EVENT,
                payload=payload,
            )
        )
    return JobOutput(
        status="ok",
        summary=f"expenses processor: {len(events)} new row(s) of {person}'s {service} kept"
        + (f"; the closed {', '.join(left_out)} books' rows left out" if left_out else ""),
        events=events,
    )


def _balances(ctx: JobContext, person: str, rows: list[dict], closed, by: str) -> list[EventSpec]:
    """The statement's closing balances, with every line it held, as one
    event (balances.py); nothing when it carried no balance, or when the
    same statement's balances are already kept."""
    open_rows = [r for r in rows if not year_close.closed_for(str(r.get("day", "")), closed)]
    days = {str(r["day"]): r["day_end"] for r in open_rows if isinstance(r.get("day_end"), int)}
    if not days:
        return []
    shas = [statements.Line.of(r).sha(person, statements.MINISTRY) for r in open_rows]
    p = balances.payload(person, days, shas, by)
    if any(balances.same(e["payload"], p) for e in _events_of(ctx, balances.BALANCE_EVENT)):
        return []
    return [
        EventSpec(
            key=balances.key(person, days, shas), event_type=balances.BALANCE_EVENT, payload=p
        )
    ]


def _school_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expschool")
    for name in ("person", "rows", "by", "said"):
        key.param(name)
    closed = sorted(year_close.read(ctx.ledger.conn, ctx.tenant_slug))
    if closed:  # a ministry that never closed a year keeps the key it had
        key.value("closed", closed)
    key.config("expenses")
    return key.digest()


def _school_run(ctx: JobContext) -> JobOutput:
    """The person's school list (t-statements): each scholarship or school
    fee a line with no receipt, as they listed it."""
    person = _filer(ctx, str(ctx.params.get("person") or ""))
    try:
        rows = [
            statements.SchoolPayment(
                str(r["day"]),
                str(r["student"]),
                str(r["school"]),
                Decimal(str(r["amount"])),
                str(r["currency"]),
                statements.OWN if r.get("paid_by") == statements.OWN else "",
            )
            for r in json.loads(str(ctx.params.get("rows") or "[]"))
        ]
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return JobOutput(status="ok", summary="expenses school: not a school list")
    if not person:
        return JobOutput(status="ok", summary="expenses school: nobody's list")
    closed = year_close.read(ctx.ledger.conn, ctx.tenant_slug)
    left_out = sorted({r.day[:4] for r in rows if year_close.closed_for(r.day, closed)})
    rows = [r for r in rows if not year_close.closed_for(r.day, closed)]
    by, said = str(ctx.params.get("by") or ""), str(ctx.params.get("said") or "")
    events = [
        EventSpec(
            key=f"school:{r.sha(person)}",
            event_type=SCHOOL_EVENT,
            payload=r.payload(person) | {"by": by, "said": said},
        )
        for r in rows
    ]
    # Each payment in another currency gets its day's rate now, so the year
    # counts it before its month is sent (the dress rehearsal, 2026-10-15).
    wants = {(r.currency, r.day) for r in rows if r.currency and r.currency != "USD"}
    events += _rates_for(wants, fx.stored(ctx.ledger.read_event_log()))
    return JobOutput(
        status="ok",
        summary=f"expenses school: {len(rows)} payment(s) for {person}"
        + (f"; the closed {', '.join(left_out)} books' payments left out" if left_out else ""),
        events=events,
    )


def _line_purpose_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "explinepurpose")
    sha = str(key.param("sha"))
    for name in ("said", "category", "by"):
        key.param(name)
    if ctx.params.get("cash"):
        key.param("cash")
    if ctx.params.get("whose"):
        key.param("whose")
    if ctx.params.get("pays_back"):
        key.param("pays_back")
    if ctx.params.get("pay_of"):
        key.param("pay_of")
    for name in ("gift_from", "fund_grant", "for_fund"):
        if ctx.params.get(name):
            key.param(name)
    key.add("before", sum(1 for e in _events_of(ctx, LINE_PURPOSE_EVENT) if _payload_sha(e) == sha))
    key.config("expenses")
    return key.digest()


def _line_purpose_run(ctx: JobContext) -> JobOutput:
    """What one statement line was for, in the person's own words, and the
    category those words point to (t-statements)."""
    sha = str(ctx.params.get("sha") or "")
    said = str(ctx.params.get("said") or "").strip()
    if not said or not any(_payload_sha(e) == sha for e in _events_of(ctx, LINE_EVENT)):
        return JobOutput(status="ok", summary="expenses line purpose: no such line")
    before = sum(1 for e in _events_of(ctx, LINE_PURPOSE_EVENT) if _payload_sha(e) == sha)
    payload = {
        "sha256": sha,
        "said": said,
        "category": str(ctx.params.get("category") or ""),
        "by": str(ctx.params.get("by") or ""),
    }
    if ctx.params.get("cash"):
        payload["cash"] = str(ctx.params["cash"])  # the rupees an ATM gave (2026-10-15)
    if ctx.params.get("whose") == "own":
        payload["whose"] = "own"  # the ministry's money spent on the person (2026-10-25)
    elif ctx.params.get("whose") == statements.PAY:
        payload["whose"] = statements.PAY  # their own account, from their own pay (c33)
    if ctx.params.get("pays_back"):
        payload["pays_back"] = str(ctx.params["pays_back"])  # whom it paid back (c28)
    if ctx.params.get("pay_of"):
        payload["pay_of"] = str(ctx.params["pay_of"])  # whose pay it was (c34)
    if ctx.params.get("gift_from"):
        payload["gift_from"] = str(ctx.params["gift_from"])  # who gave it (c32)
        if ctx.params.get("fund_grant"):
            payload["fund_grant"] = True  # a grant from a donor-advised fund (c32)
        if ctx.params.get("for_fund"):
            payload["for_fund"] = str(ctx.params["for_fund"])  # whose fund (c34 PR 3)
    return JobOutput(
        status="ok",
        summary=f"expenses line purpose: {sha[:12]} said",
        events=[
            EventSpec(
                key=f"bank-purpose:{sha}:{before}", event_type=LINE_PURPOSE_EVENT, payload=payload
            )
        ],
    )


def _line_split_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "explinesplit")
    sha = str(key.param("sha"))
    for name in ("said", "by", "take", "category", "whose"):
        key.param(name)
    key.add("before", sum(1 for e in _events_of(ctx, LINE_SPLIT_EVENT) if _payload_sha(e) == sha))
    return key.digest()


def _line_split_run(ctx: JobContext) -> JobOutput:
    """An amount out of one line, as the person said (the owner, 2026-11-10):
    a statement line, or cash gone with no receipt; the rest stays where it
    was. The door checked the kind and that the part is less than the line.
    Splits by percent (2026-10-29) stay readable in the books; none is
    written any more."""
    sha = str(ctx.params.get("sha") or "")
    said = str(ctx.params.get("said") or "").strip()
    known = [e for kind in (LINE_EVENT, CASH_USED_EVENT) for e in _events_of(ctx, kind)]
    if not said or not any(_payload_sha(e) == sha for e in known):
        return JobOutput(status="ok", summary="expenses line split: no such line")
    take = str(ctx.params.get("take") or "")
    whose = str(ctx.params.get("whose") or "")
    category = str(ctx.params.get("category") or "")
    got = statements.amount_of(take)
    if got is None or got[1] <= 0 or whose not in statements.ACCOUNTS:
        return JobOutput(status="ok", summary="expenses line split: no amount to take")
    if whose == statements.MINISTRY and not category:
        return JobOutput(status="ok", summary="expenses line split: no kind for the part")
    before = sum(1 for e in _events_of(ctx, LINE_SPLIT_EVENT) if _payload_sha(e) == sha)
    payload = {
        "sha256": sha,
        "take": take,
        "category": category if whose == statements.MINISTRY else "",
        "whose": whose,
        "said": said,
        "by": str(ctx.params.get("by") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses line split: {take} out of {sha[:12]}",
        events=[
            EventSpec(
                key=f"bank-split:{sha}:{before}", event_type=LINE_SPLIT_EVENT, payload=payload
            )
        ],
    )


def _cash_events(ctx: JobContext) -> int:
    kinds = (CASH_COUNT_EVENT, CASH_USED_EVENT, RECEIPT_SPLIT_EVENT, RODE_EVENT)
    return sum(1 for e in ctx.ledger.read_event_log() if e.get("event_type") in kinds)


def _cash_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expcash")
    for name in (
        "what",
        "sha",
        "line",
        "person",
        "day",
        "tag",
        "part",
        "whose",
        "said",
        "category",
        "by",
    ):
        key.param(name)
    key.add("before", _cash_events(ctx))
    key.config("expenses")
    return key.digest()


def _cash_run(ctx: JobContext) -> JobOutput:
    """The person's cash, in their words (t-cash-count, t-split-receipt,
    2026-10-15): what they counted in hand, cash gone with no receipt and
    whose use it was, the part of a receipt that was their own, or which
    receipt a card line carried (t-ask-which-receipt, 2026-10-16). The door
    reads every amount from their words with code; nothing here prices."""
    what = str(ctx.params.get("what") or "")
    tag = str(ctx.params.get("tag") or "")
    sha = str(ctx.params.get("sha") or "")
    if what == "rode":
        line = str(ctx.params.get("line") or "")
        if not sha or not line:
            return JobOutput(status="ok", summary="expenses cash: which line, which receipt?")
        payload = {
            "sha256": sha,
            "line": line,
            "said": str(ctx.params.get("said") or ""),
            "by": str(ctx.params.get("by") or ""),
        }
        return JobOutput(
            status="ok",
            summary=f"expenses cash: {sha[:12]} rode {line[:12]}",
            events=[
                EventSpec(
                    key=f"cash:rode:{sha}:{_cash_events(ctx)}",
                    event_type=RODE_EVENT,
                    payload=payload,
                )
            ],
        )
    if what not in ("count", "used", "split") or not _TAG.match(tag) or not sha:
        return JobOutput(status="ok", summary=f"expenses cash: nothing to record ({what})")
    said = str(ctx.params.get("said") or "")
    by = str(ctx.params.get("by") or "")
    before = _cash_events(ctx)
    if what == "split":
        part = str(ctx.params.get("part") or "")
        if not _TAG.match(part):
            return JobOutput(status="ok", summary="expenses cash: a split needs both parts")
        events = [
            EventSpec(  # the ministry's part is the receipt's amount from here on
                key=f"expamount:{AMOUNT_EVENT}:{sha}:split{before}",
                event_type=AMOUNT_EVENT,
                payload={"sha256": sha, "tag": part, "by": by, "said": said},
            ),
            EventSpec(
                key=f"cash:split:{sha}:{before}",
                event_type=RECEIPT_SPLIT_EVENT,
                payload={"sha256": sha, "own": tag, "by": by, "said": said},
            ),
        ]
        return JobOutput(status="ok", summary=f"expenses cash: {sha[:12]} split", events=events)
    payload = {
        "sha256": sha,
        "person": str(ctx.params.get("person") or ""),
        "day": str(ctx.params.get("day") or ""),
        "tag": tag,
        "said": said,
        "by": by,
    }
    kind = CASH_COUNT_EVENT
    if what == "used":
        kind = CASH_USED_EVENT
        payload |= {
            "whose": "own" if ctx.params.get("whose") == "own" else "ministry",
            "category": str(ctx.params.get("category") or ""),
        }
    return JobOutput(
        status="ok",
        summary=f"expenses cash: {what} {tag} for {payload['person']}",
        events=[EventSpec(key=f"cash:{what}:{sha}:{before}", event_type=kind, payload=payload)],
    )


def _budget_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expbudget")
    for name in ("year", "kind", "amount", "by", "said"):
        key.param(name)
    key.add("before", len(_events_of(ctx, budgets.BUDGET_EVENT)))
    key.config("expenses")
    return key.digest()


def _budget_run(ctx: JobContext) -> JobOutput:
    """A kind's budget for the year, as someone who looks over every month
    said it (t-year-budget). The newest one said is the budget."""
    year = str(ctx.params.get("year") or "")
    kind = cats.canonical(
        str(ctx.params.get("kind") or ""), list(ctx.tenant.expenses.category_accounts)
    )
    try:
        cents = int(Decimal(str(ctx.params.get("amount") or "")) * 100)
    except ArithmeticError:
        cents = -1
    if not re.fullmatch(r"\d{4}", year) or not kind or cents < 0:
        return JobOutput(status="ok", summary="expenses budget: not a budget")
    before = len(_events_of(ctx, budgets.BUDGET_EVENT))
    payload = {
        "year": year,
        "kind": kind,
        "cents": cents,
        "by": str(ctx.params.get("by") or ""),
        "said": str(ctx.params.get("said") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses budget: {kind} {year} ${cents / 100:,.2f}",
        events=[
            EventSpec(
                key=f"budget:{year}:{kind}:{before}",
                event_type=budgets.BUDGET_EVENT,
                payload=payload,
            )
        ],
    )
