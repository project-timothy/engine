"""The tools for what is due and owed: dated deadlines (and the Form 990
row), the cards waiting for a yes, vendor invoices, the scheduled runs, the
month's close, and expense reports. Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.deadlines import form990
from ..agents.deadlines.jobs import DONE_EVENT, open_occurrences, tier_for
from ..agents.deadlines.schema import load_obligations
from ..agents.expenses import statements
from ..agents.expenses.records import REVIEW_CARD
from ..agents.expenses.statements import money
from ..authority import UNDESCRIBED
from ..engine.registry import load_card_authority
from .reads import _log
from .spec import _int
from .viewer import unit_of

if TYPE_CHECKING:
    from .catalog import Tools


CLOSED_STATUSES = ("Paid", "Cancelled", "Void - Already Paid", "Void - Duplicate")


def open_deadlines(t: Tools, days: int) -> list[tuple]:
    """(obligation, due, days left, its lead days) for every open date this
    person may see within ``days``, soonest first: the deadlines tool's rows
    and the person's calendar feed (core/tools/calendar_feed.py)."""
    path = t.obligations_file
    if path is None or not Path(path).is_file():
        return []
    today = t._today()
    done = {
        (str(p.get("id")), str(p.get("due")))
        for p in (
            json.loads(r["payload_json"])
            for r in t._rows(
                "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
                (t.tenant, DONE_EVENT),
            )
        )
    }
    found = []
    for ob in load_obligations(Path(path)):
        if not t.sees("calendar", unit_of(ob.who)):
            continue
        leads = ob.leads(t.lead_days)
        for due in open_occurrences(ob, today=today, leads=[days], done=done):
            left = (due - today).days
            if left <= days:
                found.append((ob, due, left, leads))
    found.sort(key=lambda f: (f[1], f[0].id))
    return found


def _deadlines(t: Tools, args: dict) -> dict:
    days = _int(args, "days", 90, 0, 3650)
    path = t.obligations_file
    if path is None or not Path(path).is_file():
        return {"rows": [], "note": "no obligations file for this tenant"}
    rows = [
        {
            "id": ob.id,
            "title": ob.title,
            "who": ob.who,
            "kind": ob.kind,
            "due": due.isoformat(),
            "days_left": left,
            "window": tier_for(left, leads),
            "notes": ob.notes,
            "source": f"obligations.toml#{ob.id}",
            **(_form_990(t, due) if ob.id == form990.OBLIGATION_ID else {}),
        }
        for ob, due, left, leads in open_deadlines(t, days)
    ]
    return {"as_of": t._today().isoformat(), "rows": rows}


def _form_990(t: Tools, due: date) -> dict:
    """What the 990 due on ``due`` needs (t-990n): the year it covers, what
    came into the ministry's account that year, what to file, and what the
    form asks for, with what Tim already knows."""
    from ..engine.config import load_tenant

    start, end = form990.tax_year_of(due)
    gross = statements.came_in(_log(t), start, end)
    year = f"{start} to {end}"
    return {
        "tax_year": year,
        "gross_receipts": money(gross),
        "files": form990.files(gross),
        "asks_for": list(form990.ASKS_FOR),
        "known": {"legal name": load_tenant(t.tenant).identity.legal_name, "tax year": year},
    }


def _waiting_cards(t: Tools, args: dict) -> dict:
    from .decide import card_words, who_approves  # decide reaches catalog through the door

    limit = _int(args, "limit", 25)
    rows = t._rows(
        "SELECT id, agent, action_type, params_json, status, created_at FROM approval_queue "
        "WHERE tenant = ? AND status = 'pending' ORDER BY id LIMIT ?",
        (t.tenant, t._cap(limit)),
    )
    rows = [r for r in rows if _card_visible(t, r)][:limit]
    door = t.decider is not None and t.decider.offered()
    out = []
    for r in rows:
        row = {
            "card": r["id"],
            "agent": r["agent"],
            "action_type": r["action_type"],
            "status": r["status"],
            "created_at": r["created_at"],
            "params": json.loads(r["params_json"] or "{}"),
            "source": f"approval_queue#{r['id']}",
        }
        if door:  # whose yes it is (the Haiku run, A09): her own Face ID would decide it
            row["yours"] = t.decider.may_tap(t, int(r["id"]))
            row["who_approves"] = who_approves(row["yours"])
        out.append({"about": card_words(row), **row})
    return {"rows": out}


def _card_visible(t: Tools, r: sqlite3.Row) -> bool:
    """A card is seen as the thing it decides: its rule's resource, in the
    unit of the person it is about."""
    if t.viewer is None:
        return True
    rule = load_card_authority(str(r["agent"])).get(str(r["action_type"]), UNDESCRIBED)
    params = json.loads(r["params_json"] or "{}")
    unit = unit_of(str(params.get(rule.submitter, ""))) if rule.submitter else ""
    return t.sees(rule.resource, unit)


def _invoice_visible(t: Tools, r: sqlite3.Row) -> bool:
    """A vendor bill is the tenant's own unless it carries a project."""
    return t.sees("ap.invoice", str(r["project"] or ""))


def _invoice_row(r: sqlite3.Row) -> dict:
    return {
        "vendor": r["vendor"],
        "invoice_number": r["invoice_number"],
        "amount": money(r["amount_cents"]),
        "invoice_date": r["invoice_date"],
        "due_date": r["due_date"],
        "status": r["status"],
        "payment_date": r["payment_date"],
        "check_ref": r["check_ref"],
        "project": r["project"],
        "source": f"ap_invoices#{r['id']}",
    }


_INVOICE_COLS = (
    "id, vendor, invoice_number, amount_cents, invoice_date, due_date, status, "
    "payment_date, check_ref, project"
)


def _sums(rows: list[dict], by: str) -> dict:
    """What an owner asks of a list of bills, computed here so the model
    quotes it: how many, the total, and per ``by`` (vendor or status),
    largest first."""
    per: dict[str, list[Decimal]] = {}
    for r in rows:
        per.setdefault(r[by], []).append(Decimal(r["amount"]))
    groups = [
        {by: k, "count": len(amounts), "total": f"{sum(amounts, Decimal('0')):.2f}"}
        for k, amounts in per.items()
    ]
    groups.sort(key=lambda g: (-Decimal(g["total"]), str(g[by])))
    total = sum((Decimal(r["amount"]) for r in rows), Decimal("0"))
    return {"count": len(rows), "total": f"{total:.2f}", f"by_{by}": groups}


def _open_payables(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 50)
    marks = ",".join("?" for _ in CLOSED_STATUSES)
    rows = t._rows(
        f"SELECT {_INVOICE_COLS} FROM ap_invoices WHERE tenant = ? AND shadow = 0 "  # noqa: S608  (a fixed column list; values are ? marks)
        f"AND status NOT IN ({marks}) ORDER BY COALESCE(due_date, '9999'), id",
        (t.tenant, *CLOSED_STATUSES),
    )
    # The sums cover every open bill the person may see, not only the rows shown.
    seen = [_invoice_row(r) for r in rows if _invoice_visible(t, r)]
    return {"rows": seen[:limit], **_sums(seen, "vendor")}


def _find_invoices(t: Tools, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("find_invoices needs a query (a vendor or an invoice number)")
    limit = _int(args, "limit", 25)
    like = f"%{query}%"
    rows = t._rows(
        f"SELECT {_INVOICE_COLS} FROM ap_invoices WHERE tenant = ? AND shadow = 0 "  # noqa: S608  (a fixed column list; values are ? marks)
        "AND (vendor LIKE ? OR invoice_number LIKE ?) ORDER BY invoice_date DESC, id DESC",
        (t.tenant, like, like),
    )
    seen = [_invoice_row(r) for r in rows if _invoice_visible(t, r)]
    return {"rows": seen[:limit], **_sums(seen, "status")}


def _recent_runs(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 40)
    rows = t._rows(
        "SELECT id, agent, job, status, summary, created_at FROM runs WHERE id IN ("
        " SELECT MAX(id) FROM runs WHERE tenant = ? AND shadow = 0 GROUP BY agent, job)"
        " ORDER BY created_at DESC LIMIT ?",
        (t.tenant, limit),
    )
    return {
        "rows": [
            {
                "agent": r["agent"],
                "job": r["job"],
                "status": r["status"],
                "when": r["created_at"],
                "summary": (r["summary"] or "")[:300],
                "source": f"runs#{r['id']}",
            }
            for r in rows
        ]
    }


def _close_status(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 12)
    rows = t._rows(
        "SELECT id, event_type, payload_json, created_at FROM events "
        "WHERE tenant = ? AND event_type LIKE 'close.%' ORDER BY id DESC LIMIT ?",
        (t.tenant, limit),
    )
    events = []
    for r in rows:
        payload = json.loads(r["payload_json"] or "{}")
        events.append(
            {
                "event": r["event_type"],
                "month": payload.get("month"),
                "when": r["created_at"],
                "source": f"events#{r['id']}",
            }
        )
    locked = [
        json.loads(r["payload_json"] or "{}").get("month")
        for r in t._rows(
            "SELECT payload_json FROM events WHERE tenant = ? AND event_type = 'close.locked'",
            (t.tenant,),
        )
    ]
    locked = [m for m in locked if m]
    return {"last_locked_month": max(locked) if locked else None, "rows": events}


def _reports_from_cards(t: Tools, limit: int) -> list[dict]:
    """Reports with no expense_report row yet, read from their review cards:
    one waiting for a yes, or one approved that the next expenses run has
    not filed (2026-10-12). Per person and month, the newest card speaks."""
    cards = t._rows(
        "SELECT id, agent, action_type, status, params_json FROM approval_queue "
        "WHERE tenant = ? AND status IN ('pending', 'approved') AND action_type = ? "
        "ORDER BY id DESC",
        (t.tenant, REVIEW_CARD),
    )
    filed = {
        (r["person"], r["month"])
        for r in t._rows(
            "SELECT person, month FROM expense_report WHERE tenant = ? AND shadow = 0",
            (t.tenant,),
        )
    }
    seen: set[tuple] = set()
    out = []
    for r in cards:
        params = json.loads(r["params_json"] or "{}")
        report = (params.get("person"), params.get("month"))
        if report in seen or not _card_visible(t, r):
            continue
        seen.add(report)
        if r["status"] == "approved" and report in filed:
            continue
        cents = str(params.get("total_cents", ""))
        out.append(
            {
                "person": params.get("person"),
                "month": params.get("month"),
                "total": money(int(cents)) if cents.lstrip("-").isdigit() else None,
                "status": "Waiting for approval" if r["status"] == "pending" else "Approved",
                "reimbursed_date": None,
                "cleared_date": None,
                "source": f"approval_queue#{r['id']}",
            }
        )
    return out[:limit]


def _expense_reports(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 20)
    waiting = _reports_from_cards(t, limit)
    rows = t._rows(
        "SELECT id, person, month, total_cents, status, reimbursed_date, cleared_date "
        "FROM expense_report WHERE tenant = ? AND shadow = 0 ORDER BY id DESC LIMIT ?",
        (t.tenant, t._cap(limit)),
    )
    rows = [r for r in rows if t.sees("expense.report", unit_of(r["person"]))]
    return {
        "rows": waiting
        + [
            {
                "person": r["person"],
                "month": r["month"],
                "total": money(r["total_cents"]),
                "status": r["status"],
                "reimbursed_date": r["reimbursed_date"],
                "cleared_date": r["cleared_date"],
                "source": f"expense_report#{r['id']}",
            }
            for r in rows
        ][: max(limit - len(waiting), 0)]
    }
