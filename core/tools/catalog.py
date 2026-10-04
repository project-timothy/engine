"""The engine's read-only tools: what a chat front end may ask the books.

Ask Tim, step 1 (the design note of 2026-10-04). Each tool answers one plain
question from the ledger: what is due, what is waiting for a yes, what is
still owed, did this morning's run work. Boundary classification
(docs/boundary-rules.md): **code**. A model may choose which tool to call and
explain the answer; the answer itself is computed here, every money value is
an exact decimal string, and every row names its source so it can be traced
to the books. Nothing here writes: the ledger is opened read-only and the run
lock is never taken, so a question mid-run neither blocks nor is blocked.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..agents.deadlines.jobs import DONE_EVENT, open_occurrences, tier_for
from ..agents.deadlines.schema import DEFAULT_LEAD_DAYS, load_obligations

CLOSED_STATUSES = ("Paid", "Cancelled", "Void - Already Paid", "Void - Duplicate")


def money(cents: int | None) -> str | None:
    """Exact dollars as a string; a float never carries money."""
    return None if cents is None else f"{Decimal(int(cents)) / 100:.2f}"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    fn: Callable[[Tools, dict], dict]


def _int(args: dict, key: str, default: int, lo: int = 1, hi: int = 500) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


@dataclass
class Tools:
    tenant: str
    ledger_root: Path
    obligations_file: Path | None = None
    today: str | None = None
    lead_days: tuple[int, ...] = DEFAULT_LEAD_DAYS
    _specs: dict[str, ToolSpec] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._specs = {spec.name: spec for spec in SPECS}

    # -- plumbing --------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def call(self, name: str, args: dict | None = None) -> dict:
        if name not in self._specs:
            raise KeyError(f"no tool {name!r}; tools: {', '.join(TOOL_NAMES)}")
        return self._specs[name].fn(self, dict(args or {}))

    def _db(self) -> sqlite3.Connection:
        path = Path(self.ledger_root) / "ledger.sqlite3"
        if not path.is_file():
            raise FileNotFoundError(f"no ledger for {self.tenant} at {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        con = self._db()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def _today(self) -> date:
        return date.fromisoformat(self.today) if self.today else date.today()


# ---- the tools ---------------------------------------------------------------------


def _deadlines(t: Tools, args: dict) -> dict:
    days = _int(args, "days", 90, 0, 3650)
    path = t.obligations_file
    if path is None or not Path(path).is_file():
        return {"rows": [], "note": "no obligations file for this tenant"}
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
    rows = []
    for ob in load_obligations(Path(path)):
        leads = ob.leads(t.lead_days)
        for due in open_occurrences(ob, today=today, leads=[days], done=done):
            left = (due - today).days
            if left > days:
                continue
            rows.append(
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
                }
            )
    rows.sort(key=lambda r: (r["due"], r["id"]))
    return {"as_of": today.isoformat(), "rows": rows}


def _waiting_cards(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 25)
    rows = t._rows(
        "SELECT id, agent, action_type, params_json, status, created_at FROM approval_queue "
        "WHERE tenant = ? AND status = 'pending' ORDER BY id LIMIT ?",
        (t.tenant, limit),
    )
    return {
        "rows": [
            {
                "card": r["id"],
                "agent": r["agent"],
                "action_type": r["action_type"],
                "status": r["status"],
                "created_at": r["created_at"],
                "params": json.loads(r["params_json"] or "{}"),
                "source": f"approval_queue#{r['id']}",
            }
            for r in rows
        ]
    }


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


def _open_payables(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 50)
    marks = ",".join("?" for _ in CLOSED_STATUSES)
    rows = t._rows(
        f"SELECT {_INVOICE_COLS} FROM ap_invoices WHERE tenant = ? AND shadow = 0 "
        f"AND status NOT IN ({marks}) ORDER BY COALESCE(due_date, '9999'), id LIMIT ?",
        (t.tenant, *CLOSED_STATUSES, limit),
    )
    out = [_invoice_row(r) for r in rows]
    total = sum((Decimal(r["amount"]) for r in out), Decimal("0"))
    return {"rows": out, "total": f"{total:.2f}"}


def _find_invoices(t: Tools, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("find_invoices needs a query (a vendor or an invoice number)")
    limit = _int(args, "limit", 25)
    like = f"%{query}%"
    rows = t._rows(
        f"SELECT {_INVOICE_COLS} FROM ap_invoices WHERE tenant = ? AND shadow = 0 "
        "AND (vendor LIKE ? OR invoice_number LIKE ?) ORDER BY invoice_date DESC, id DESC LIMIT ?",
        (t.tenant, like, like, limit),
    )
    return {"rows": [_invoice_row(r) for r in rows]}


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


def _expense_reports(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 20)
    rows = t._rows(
        "SELECT id, person, month, total_cents, status, reimbursed_date, cleared_date "
        "FROM expense_report WHERE tenant = ? AND shadow = 0 ORDER BY id DESC LIMIT ?",
        (t.tenant, limit),
    )
    return {
        "rows": [
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
        ]
    }


def _schema(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
        "additionalProperties": False,
    }


_LIMIT = {"limit": {"type": "integer", "minimum": 1, "maximum": 500}}

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "deadlines",
        "Dated obligations (filings, renewals, reports) due within the next N days, "
        "from the tenant's obligations file, minus any marked done.",
        _schema({"days": {"type": "integer", "minimum": 0, "maximum": 3650}}),
        _deadlines,
    ),
    ToolSpec(
        "waiting_cards",
        "Approval cards still waiting for a yes or no.",
        _schema(_LIMIT),
        _waiting_cards,
    ),
    ToolSpec(
        "open_payables",
        "Vendor invoices not yet paid, earliest due first, with the total owed.",
        _schema(_LIMIT),
        _open_payables,
    ),
    ToolSpec(
        "find_invoices",
        "Find vendor invoices by vendor name or invoice number (any status).",
        _schema({"query": {"type": "string"}, **_LIMIT}, ["query"]),
        _find_invoices,
    ),
    ToolSpec(
        "recent_runs",
        "The latest run of every scheduled job and how it ended: did this morning's run work.",
        _schema(_LIMIT),
        _recent_runs,
    ),
    ToolSpec(
        "close_status",
        "Month-end close progress: the last month sealed and the latest close events.",
        _schema(_LIMIT),
        _close_status,
    ),
    ToolSpec(
        "expense_reports",
        "Expense reports with their totals and where each stands (reimbursed, cleared).",
        _schema(_LIMIT),
        _expense_reports,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(s.name for s in SPECS)
