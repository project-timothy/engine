"""The engine's read-only tools: what a chat front end may ask the books.

Ask Tim, step 1 (the design note of 2026-10-04). Each tool answers one plain
question from the ledger: what is due, what is waiting for a yes, what is
still owed, did this morning's run work. Boundary classification
(docs/boundary-rules.md): **code**. A model may choose which tool to call and
explain the answer; the answer itself is computed here, every money value is
an exact decimal string, and every row names its source so it can be traced
to the books. Nothing here writes: the ledger is opened read-only and the run
lock is never taken, so a question mid-run neither blocks nor is blocked. The
one exception is ``decide_card``, offered only on the box's door with a
doorkeeper (``decider``), which writes through core/tools/decide.py and only
on the person's own Face ID.

With a ``viewer`` (core/tools/viewer.py), the tools answer as that person:
each row reaches them only when authority.toml lets them view its resource
in its unit, totals count only those rows, and a tool whose rows they could
never see is not offered. Without one, they answer for the whole tenant (the
owner's own local use).
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
from ..authority import UNDESCRIBED
from ..engine.registry import load_card_authority
from .viewer import Viewer, unit_of

CLOSED_STATUSES = ("Paid", "Cancelled", "Void - Already Paid", "Void - Duplicate")


def money(cents: int | None) -> str | None:
    """Exact dollars as a string; a float never carries money."""
    return None if cents is None else f"{Decimal(int(cents)) / 100:.2f}"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    fn: Callable[[Any, dict], dict]
    annotations: dict | None = None  # MCP tool hints; None = read-only, closed world


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
    viewer: Viewer | None = None
    decider: Any = None  # core/tools/decide.Decider, on the box's door only
    _specs: dict[str, ToolSpec] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._specs = {
            spec.name: spec for spec in SPECS if spec.name not in BOOKS_TOOLS or self.sees("books")
        }
        if self.decider is not None and self.decider.offered():
            self._specs[DECIDE.name] = DECIDE

    # -- plumbing --------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def call(self, name: str, args: dict | None = None) -> dict:
        if name not in self._specs:
            raise KeyError(f"no tool {name!r}; tools: {', '.join(self._specs)}")
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

    def sees(self, resource: str, unit: str = "") -> bool:
        """Whether the person asking may view this row; always, with no viewer."""
        return self.viewer is None or self.viewer.sees(resource, unit)

    def _cap(self, limit: int) -> int:
        """The SQL LIMIT: the caller's, or none when rows are filtered after
        the query (a limit before the filter could hide what the person may
        see behind rows they may not)."""
        return limit if self.viewer is None else -1


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
        if not t.sees("calendar", unit_of(ob.who)):
            continue
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
        (t.tenant, t._cap(limit)),
    )
    rows = [r for r in rows if _card_visible(t, r)][:limit]
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


def _open_payables(t: Tools, args: dict) -> dict:
    limit = _int(args, "limit", 50)
    marks = ",".join("?" for _ in CLOSED_STATUSES)
    rows = t._rows(
        f"SELECT {_INVOICE_COLS} FROM ap_invoices WHERE tenant = ? AND shadow = 0 "
        f"AND status NOT IN ({marks}) ORDER BY COALESCE(due_date, '9999'), id LIMIT ?",
        (t.tenant, *CLOSED_STATUSES, t._cap(limit)),
    )
    out = [_invoice_row(r) for r in rows if _invoice_visible(t, r)][:limit]
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
        (t.tenant, like, like, t._cap(limit)),
    )
    return {"rows": [_invoice_row(r) for r in rows if _invoice_visible(t, r)][:limit]}


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
        (t.tenant, t._cap(limit)),
    )
    rows = [r for r in rows if t.sees("expense.report", unit_of(r["person"]))][:limit]
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


def _decide(t: Tools, args: dict) -> dict:
    return t.decider.decide(t, args)


DECIDE = ToolSpec(
    "decide_card",
    "Approve or reject one waiting card as the signed-in person. The first call returns a "
    "link: the person opens it on their phone, reads the card, and taps the button with Face "
    "ID or a fingerprint. Call again with the same card and decision once they say they're "
    "done, and the card is decided. Nothing is decided without their own tap.",
    _schema(
        {
            "card": {"type": "integer", "minimum": 1},
            "decision": {"type": "string", "enum": ["approve", "reject"]},
        },
        ["card", "decision"],
    ),
    _decide,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)

BOOKS_TOOLS = frozenset({"recent_runs", "close_status"})
"""Tools about the books as a whole (the runs, the month's lock): offered
only to a person who may view the books in every unit."""
