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
from ..agents.expenses import fx, records, statements
from ..agents.expenses.categories import UNCATEGORIZED, canonical, from_words
from ..agents.expenses.jobs import (
    AMOUNT_EVENT,
    CATEGORY_EVENT,
    DATED_EVENT,
    DRIVE_EVENT,
    HELD_EVENT,
    LANDED_EVENT,
    PRICED_EVENT,
    PROPOSED_EVENT,
    PURPOSE_EVENT,
    READ_EVENT,
    REFERENCE_EVENT,
    REVIEW_CARD,
    SEND_CARD,
    SENT_EVENT,
    amounts_said,
    claim_name,
    drive_id,
)
from ..agents.expenses.schema import parse_amount_tag
from ..agents.policy.jobs import approved_rule_shas
from ..authority import UNDESCRIBED, Request, evaluate
from ..engine.registry import load_card_authority
from ..policy import Rulebook, Spent, check_month
from .receipts import _PLACERS, from_tag
from .viewer import Viewer, display_name, unit_of

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
    receipts: Any = None  # core/tools/receipts.Receipts, on the box's door only
    ministry: str = ""  # the ministry's name, said back by start_here
    _specs: dict[str, ToolSpec] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._specs = {
            spec.name: spec for spec in SPECS if spec.name not in BOOKS_TOOLS or self.sees("books")
        }
        if self.decider is not None and self.decider.offered():
            self._specs[DECIDE.name] = DECIDE
        if _rulebook(self) is not None:  # only a ministry that wrote rules down
            self._specs[ASK_THE_RULES.name] = ASK_THE_RULES
        if self.receipts is not None and self.receipts.offered():
            self._specs[ADD_RECEIPT.name] = ADD_RECEIPT
        if self.receipts is not None and self.receipts.sorts(self.viewer):
            self._specs[SET_CATEGORY.name] = SET_CATEGORY
            self._specs[SET_AMOUNT.name] = SET_AMOUNT
            self._specs[SET_DRIVE_PURPOSE.name] = SET_DRIVE_PURPOSE
            self._specs[CATCH_UP_LINK.name] = CATCH_UP_LINK
            self._specs[SET_RECEIPT_DAY.name] = SET_RECEIPT_DAY
            self._specs[RECEIPTS_TO_READ.name] = RECEIPTS_TO_READ
            self._specs[RECORD_READING.name] = RECORD_READING
            self._specs[AGREE_TO_READINGS.name] = AGREE_TO_READINGS
            self._specs[SEND_MONTH_FOR_REVIEW.name] = SEND_MONTH_FOR_REVIEW
            for spec in STATEMENT_TOOLS:
                self._specs[spec.name] = spec

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
    from .decide import card_words  # decide reaches catalog through the door

    limit = _int(args, "limit", 25)
    rows = t._rows(
        "SELECT id, agent, action_type, params_json, status, created_at FROM approval_queue "
        "WHERE tenant = ? AND status = 'pending' ORDER BY id LIMIT ?",
        (t.tenant, t._cap(limit)),
    )
    rows = [r for r in rows if _card_visible(t, r)][:limit]
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


def _events(t: Tools, *types: str) -> list[tuple[int, dict]]:
    marks = ", ".join("?" for _ in types)
    rows = t._rows(
        f"SELECT id, event_type, payload_json FROM events WHERE tenant = ? "  # noqa: S608  (a fixed column list; values are ? marks)
        f"AND event_type IN ({marks}) ORDER BY id",
        (t.tenant, *types),
    )
    return [
        (r["id"], {"event_type": r["event_type"], **json.loads(r["payload_json"] or "{}")})
        for r in rows
    ]


def _said(line: dict) -> str | None:
    """A foreign amount the way the person said it, with its code."""
    if not line.get("said_currency"):
        return None
    return f"{line['said_currency']} {Decimal(line['said_amount']):,.2f}"


def _rate_words(line: dict) -> str | None:
    if not line.get("fx_rate"):
        return None
    per = f"{Decimal(line['fx_rate']):,.2f}"
    return f"{line['said_currency']} {per} to the dollar on {line['fx_date']}"


def _month_line(line: dict, source: str) -> dict:
    cents = line.get("amount_cents")
    return {
        "date": line.get("date") or None,
        "said": _said(line),
        "dollars": money(cents) if cents is not None else None,
        "rate": _rate_words(line),
        "rate_from": line.get("fx_source") or None,
        "category": line.get("category") or UNCATEGORIZED,
        "fund": line.get("project") or None,
        "read": line.get("read") or None,
        "waiting_on": "the day's exchange rate"
        if cents is None and line.get("said_currency")
        else line.get("unpriced") or None,
        "source": source,
    }


@dataclass
class _Sending:
    """Where each receipt stands with the person's institution, for a
    ministry whose claims go there (the send job's cards and events): sent,
    held, going on the next run, waiting for a yes, or not sent yet."""

    t: Tools
    who: str
    states: dict[str, tuple[str, str]]
    cards: dict[str, str]

    @classmethod
    def read(cls, t: Tools) -> _Sending | None:
        rows = t._rows(
            "SELECT status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
            "ORDER BY id",
            (t.tenant, SEND_CARD),
        )
        if not rows:
            return None
        cards, who = {}, ""
        for r in rows:
            params = json.loads(r["params_json"] or "{}")
            who = str(params.get("institution") or who)
            if r["status"] in ("pending", "approved"):
                cards.update(dict.fromkeys(str(params.get("receipts", "")).split(","), r["status"]))
        states = {
            str(e.get("receipt")): (e["event_type"], str(e.get("say", "")))
            for _i, e in _events(t, SENT_EVENT, HELD_EVENT)
        }
        return cls(t, who or "the institution", states, cards)

    def _yes(self, person: str) -> str:
        mine = self.t.viewer is not None and self.t.viewer.person == unit_of(person)
        return "your yes" if mine else f"{person}'s yes"

    def where(self, sha: str, person: str) -> str:
        kind, say = self.states.get(sha, ("", ""))
        if kind == SENT_EVENT:
            return f"Sent to {self.who}"
        if kind == HELD_EVENT:
            return say
        if self.cards.get(sha) == "approved":
            return f"Going to {self.who} on the next run"
        if self.cards.get(sha) == "pending":
            return f"Waiting for {self._yes(person)}"
        return "Not sent yet"

    def review(self, person: str) -> str:
        return f"Goes to {self.who} on {self._yes(person)}"


def _amounts(t: Tools) -> tuple[dict[str, str], dict[str, dict]]:
    """Door receipts' amounts after the photo: said by the person (a tag),
    and read off the photo (a proposal: its tag, and who it reads as paid
    and for what, when the reading said)."""
    logged = [
        {"event_type": e["event_type"], "payload": e}
        for _i, e in _events(t, AMOUNT_EVENT, READ_EVENT)
    ]
    readings = {
        str(e["payload"]["sha256"]): e["payload"]
        for e in logged
        if e["event_type"] == READ_EVENT and e["payload"].get("sha256")
    }
    return amounts_said(logged), readings


def _said_name(said: dict[str, str], sha: str, name: str) -> str:
    return claim_name(name, said[sha]) if sha in said else name


def _unread(sha: str, name: str, cents: int | None, readings: dict[str, dict]) -> dict:
    """What a receipt sent without an amount is waiting on, and what its
    photo reads when it was read ("$18.75 at Shoprite Maputo, Meals")."""
    if cents is not None or not ("_receipt_unread" in name or records.UNDATED in name):
        return {}
    if sha not in readings:
        return {"unpriced": "how much it was"}
    reading = readings[sha]
    words = from_tag(str(reading["tag"])).words()
    if reading.get("payee"):
        words += f" at {reading['payee']}"
    if reading.get("category"):
        words += f", {reading['category']}"
    return {"read": words, "unpriced": "their word on the amount"}


def _month_drives(t: Tools, month: str) -> dict[str, list[dict]]:
    """Each person's drives in the month (core/mileage): miles, what the
    person said it was for, and its dollars at the rate on its own day when
    it was the ministry's. A drive nobody spoke for waits on their word."""
    from ..engine.config import load_tenant
    from ..institution import mileage_rate

    said = {str(e.get("drive")): e for _i, e in _events(t, PURPOSE_EVENT)}
    settings = load_tenant(t.tenant).institution
    filed = {
        str(r["receipt_sha256"])
        for r in t._rows("SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,))
    }
    out: dict[str, list[dict]] = {}
    for eid, e in _events(t, DRIVE_EVENT):
        day = str(e.get("start", ""))[:10]
        if day[:7] != month or records.drive_sha(drive_id(e)) in filed:
            continue  # a filed drive is a line of the month's report
        word = said.get(drive_id(e), {})
        out.setdefault(str(e.get("person")), []).append(
            _drive_line(e, word, settings, day, f"events#{eid}", mileage_rate)
        )
    return out


def _drive_line(e: dict, word: dict, settings: Any, day: str, source: str, rate_of: Any) -> dict:
    miles = Decimal(str(e.get("miles", "0")))
    ministry = bool(word.get("ministry")) if word else None
    rate, rate_from = rate_of(settings, date.fromisoformat(day))
    dollars = records.drive_dollars(miles, rate)
    return {
        "date": day,
        "miles": str(miles),
        "for": word.get("said"),
        "ministry": ministry,
        "dollars": f"{dollars:.2f}" if ministry else None,
        "rate": str(rate) if ministry else None,
        "rate_from": rate_from if ministry else None,
        "waiting_on": None if word else "what it was for",
        "source": source,
    }


def _records(t: Tools) -> dict[str, records.Record]:
    """Where each receipt stands, read from the books (records.where)."""
    events = [
        {
            "event_type": r["event_type"],
            "created_at": r["created_at"],
            "payload": json.loads(r["payload_json"] or "{}"),
        }
        for r in t._rows(
            "SELECT event_type, payload_json, created_at FROM events WHERE tenant = ? ORDER BY id",
            (t.tenant,),
        )
    ]
    cards = [
        {**dict(r), "params": json.loads(r["params_json"] or "{}")}
        for r in t._rows(
            "SELECT id, action_type, status, params_json, created_at, resolved_at "
            "FROM approval_queue WHERE tenant = ? AND action_type IN (?, ?) ORDER BY id",
            (t.tenant, REVIEW_CARD, SEND_CARD),
        )
    ]
    lines = t._rows(
        "SELECT l.receipt_sha256, r.status FROM expense_line l "
        "JOIN expense_report r ON r.id = l.report_id WHERE r.tenant = ? AND r.shadow = 0",
        (t.tenant,),
    )
    return records.where(events, cards, [dict(r) for r in lines])


def _review_stands(t: Tools, month: str) -> dict[str, str]:
    """Where each person's month stands with its reviewer: a card waiting,
    approved, or the report's own status once built."""
    stands: dict[str, str] = {}
    for r in t._rows(
        "SELECT id, status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status IN ('pending', 'approved') ORDER BY id",
        (t.tenant, REVIEW_CARD),
    ):
        params = json.loads(r["params_json"] or "{}")
        if params.get("month") == month:
            stands[params.get("person")] = (
                "Waiting for approval" if r["status"] == "pending" else "Approved"
            )
    for r in t._rows(
        "SELECT person, status FROM expense_report WHERE tenant = ? AND month = ? AND shadow = 0",
        (t.tenant, month),
    ):
        stands[r["person"]] = r["status"]
    return stands


def _rules_say(t: Tools, month: str) -> dict[str, list[str]]:
    """What the ministry's rules in force say about each person's month, as
    their newest review card carries it (core/agents/policy: the card is the
    one place it is worked out, so the card, the page and Tim agree)."""
    said: dict[str, list[str]] = {}
    for r in t._rows(
        "SELECT params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status IN ('pending', 'approved') ORDER BY id",
        (t.tenant, REVIEW_CARD),
    ):
        params = json.loads(r["params_json"] or "{}")
        if params.get("month") == month and "policy" in params:
            words = str(params["policy"])
            said[str(params.get("person"))] = [] if words == "none" else words.split("; ")
    return said


def _rulebook(t: Tools) -> Rulebook | None:
    """The ministry's rules as written (core/policy), drafts and all; None
    when it has none or they do not load (the month card says why)."""
    from ..engine.authority_gate import load_policy

    try:
        authority = load_policy(t.tenant)
    except Exception:  # noqa: BLE001  (a tool list never fails on a policy file)
        return None
    return authority.rules if authority is not None and not authority.rules_error else None


def _ask_the_rules(t: Tools, args: dict) -> dict:
    """Before the spend (chunk 5): what the ministry's rules in force say
    about this kind of spending at this amount, each in its own words with
    its page. Advice only; the same check runs on the month card after."""
    from ..engine.config import load_tenant

    book = _rulebook(t)
    con = t._db()
    try:
        approved = approved_rule_shas(con, t.tenant)
    finally:
        con.close()
    in_force = [r for r in (book.rules if book else []) if r.sha in approved]
    what = str(args.get("what") or "").strip()
    if not in_force:
        return {
            "kind": "",
            "rules": [],
            "answer": "Your ministry has rules written down, but none of them is in force yet: "
            "each one waits for a yes from whoever approves them.",
        }
    listed = list(load_tenant(t.tenant).expenses.category_accounts)
    kind = canonical(str(args.get("kind") or ""), listed) or from_words(what, listed)
    if not kind:
        return {
            "kind": "",
            "rules": [],
            "answer": f"Which of these is it: {', '.join(listed)}? The rules differ by kind.",
        }
    me = t.viewer.person if t.viewer is not None else ""
    held = t.viewer.policy.people.get(me) if t.viewer is not None else None
    roles = held.roles if held else ()
    covering = [r for r in in_force if r.covers(me, roles, kind)]
    raw = str(args.get("amount") or "").replace("$", "").replace(",", "").strip()
    try:
        cents = int(Decimal(raw) * 100) if raw else None
    except ArithmeticError:
        cents = None
    applies: set[str] = set()
    if cents is not None:
        ask = Spent("ask", what or kind, kind, cents, receipt=False)
        found = check_month(covering, [ask], person=me, roles=roles)
        applies = {f.rule.sha for f in found}
    rules = [
        {
            "says": r.says,
            "from": r.cite(),
            "quote": r.source.quote,
            "applies": None if cents is None else r.sha in applies,
        }
        for r in covering
    ]
    said = [f"{r['says']} ({r['from']})" for r in rules if r["applies"]]
    rest = [f"{r['says']} ({r['from']})" for r in rules if not r["applies"]]
    if cents is None:
        answer = f"Your ministry's rules for {kind}: " + " ".join(rest) if rest else ""
    elif said:
        answer = " ".join(said)
    else:
        answer = (
            f"Nothing in your ministry's rules stands in the way of ${cents / 100:,.2f} for {kind}."
        )
        if rest:
            answer += " Keep in mind: " + " ".join(rest)
    return {
        "kind": kind,
        "rules": rules,
        "answer": answer or f"Your ministry has no rules about {kind}.",
    }


def _with_rules_say(people: list[dict], said: dict[str, list[str]]) -> list[dict]:
    """Each person's month with what the rules say, only where they say
    something: a ministry with no rules in force reads as it always did."""
    return [p | {"rules_say": said[p["person"]]} if said.get(p["person"]) else p for p in people]


def _month_report(t: Tools, args: dict) -> dict:
    """The missionary's month (alpha step 3): each person's receipts for the
    month, a foreign one shown as said beside its dollar value at the rate
    on its own date, the dollar total, and where the review stands. Read
    only: a rate not yet recorded shows as waiting, never guessed."""
    month = str(args.get("month") or t._today().strftime("%Y-%m"))
    who = str(args.get("person") or "").strip()
    rates = fx.stored(
        [{"event_type": e["event_type"], "payload": e} for _i, e in _events(t, fx.RATE_EVENT)]
    )
    people: dict[str, list[dict]] = {}

    filed = t._rows(
        "SELECT l.*, r.person FROM expense_line l JOIN expense_report r ON r.id = l.report_id "
        "WHERE r.tenant = ? AND r.month = ? AND r.shadow = 0 ORDER BY l.id",
        (t.tenant, month),
    )
    consumed = {
        str(r["receipt_sha256"])
        for r in t._rows("SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,))
    }
    stands = _records(t)
    where = stands  # every record's stage; ``stands`` is reused below for the reviews
    log = _log(t)
    banked = {  # a filed statement line or school payment reads once, as a bank line
        str(e["payload"].get("sha256"))
        for e in log
        if e["event_type"] in (statements.LINE_EVENT, statements.SCHOOL_EVENT)
    }
    for r in filed:
        if str(r["receipt_sha256"]) in banked:
            continue
        line = dict(r)
        line["date"] = line["fx_date"] or line["expense_date"] or str(line["receipt_file"])[:10]
        shown = _month_line(line, f"expense_line#{r['id']}")
        rec = stands.get(str(r["receipt_sha256"]))
        shown["stands"] = rec.stage if rec else "reported"
        people.setdefault(r["person"], []).append(shown)

    read: dict[str, int] = {}
    placed: dict[str, str] = {}
    for _i, e in _events(t, PROPOSED_EVENT, PRICED_EVENT):
        if e.get("amount_cents") is not None:
            read[str(e.get("sha256"))] = int(e["amount_cents"])
        if e.get("category"):
            placed[str(e.get("sha256"))] = str(e["category"])
    for _i, e in _events(t, CATEGORY_EVENT):
        placed[str(e.get("sha256"))] = str(e.get("category", ""))
    skip = (
        consumed
        | {str(e.get("sha256")) for _i, e in _events(t, REFERENCE_EVENT)}
        | statements.attached(log)  # a receipt behind a bank line rides that line
    )
    sending = _Sending.read(t)
    said, readings = _amounts(t)
    dated = {str(e.get("sha256")): str(e.get("day")) for _i, e in _events(t, DATED_EVENT)}
    for eid, e in _events(t, LANDED_EVENT):
        sha = str(e.get("sha256", ""))
        sent_as = str(e.get("file", ""))
        name = _said_name(said, sha, records.dated_name(sent_as, dated.get(sha, "")))
        if (dated[sha][:7] if sha in dated else str(e.get("month"))) != month or sha in skip:
            continue
        skip.add(sha)
        undated = records.UNDATED in sent_as and sha not in dated
        line = {
            "date": name[:10] if name[:4].isdigit() and not undated else None,
            "category": placed.get(sha, ""),
            "project": e.get("project", ""),
        }
        line.update(fx.priced(name, rates))
        if "amount_cents" not in line:
            line["amount_cents"] = read.get(sha, parse_amount_tag(name))
        line.update(_unread(sha, name, line["amount_cents"], readings))
        if undated:
            line["unpriced"] = "the receipt's day"
        shown = _month_line(line, f"events#{eid}")
        shown["stands"] = stands[sha].stage if sha in stands else "captured"
        if sending is not None:
            shown["sent"] = sending.where(sha, str(e.get("person")))
        people.setdefault(str(e.get("person")), []).append(shown)

    stands = _review_stands(t, month)

    drives = _month_drives(t, month)
    bank = _month_bank(t, month, log, consumed, where)
    for person in [*drives, *bank]:
        people.setdefault(person, [])
    out = []
    for person, lines in sorted(people.items()):
        if who and who.casefold() not in person.casefold():
            continue
        if not t.sees("expense.report", unit_of(person)):
            continue
        if len({ln["fund"] for ln in lines if ln["fund"]}) < 2:
            for ln in lines:  # one fund is never named on every line (10/14)
                ln["fund"] = None
        priced = [ln for ln in lines if ln["dollars"] is not None]
        mine = bank.get(person, [])
        owed = [b for b in mine if b["role"] == statements.OWED and b["dollars"] is not None]
        total = sum(int(Decimal(ln["dollars"]) * 100) for ln in [*priced, *owed])
        out.append(
            {
                "person": person,
                "month": month,
                "total": money(total),
                "receipts": len(lines),
                "waiting_on_a_rate": len(lines) - len(priced),
                "review": sending.review(person)
                if sending is not None
                else stands.get(person, "Not yet sent for review"),
                "lines": lines,
                "drives": drives.get(person, []),
                "bank": mine,
                "bank_waiting": sum(1 for b in mine if b["waiting_on"]),
            }
        )
    return {"month": month, "people": _with_rules_say(out, _rules_say(t, month))}


def _log(t: Tools) -> list[dict]:
    return [
        {
            "event_type": r["event_type"],
            "created_at": r["created_at"],
            "payload": json.loads(r["payload_json"] or "{}"),
        }
        for r in t._rows(
            "SELECT event_type, payload_json, created_at FROM events WHERE tenant = ? ORDER BY id",
            (t.tenant,),
        )
    ]


def _month_bank(
    t: Tools, month: str, log: list[dict], reported: set[str], stands: dict
) -> dict[str, list[dict]]:
    """Each person's statement lines and school payments in the month
    (t-statements): what each is, its dollars, whether a receipt stands
    behind it, and what it still waits on. Read through the same code as
    the month's card (statements.month_lines)."""
    from ..engine.config import load_tenant

    listed = list(load_tenant(t.tenant).expenses.category_accounts)
    out: dict[str, list[dict]] = {}
    for ln in statements.month_lines(log, month, reported=reported, categories=listed):
        rec = stands.get(ln["sha256"])
        out.setdefault(ln["person"], []).append(
            {
                "date": ln["day"],
                "words": ln["words"],
                "dollars": money(ln["cents"]) if ln["cents"] is not None else None,
                "role": ln["role"],
                "account": ln["account"] or None,
                "said": _said(ln),
                "rate": _rate_words(ln),
                "category": ln["category"] or None,
                "receipt": "attached" if ln["receipt"] else None,
                "for": ln["said"] or None,
                "waiting_on": ln["waiting_on"] or None,
                "stands": rec.stage if rec else "captured",
                "source": f"bank#{ln['sha256'][:16]}",
            }
        )
    return out


def _year_report(t: Tools, args: dict) -> dict:
    """Where's my year: each month's receipts, total, what still waits, and
    where its review stands; totals by category; and receipts sent in a
    catch-up that are in no month yet. Read only."""
    year = str(args.get("year") or t._today().year)
    who = str(args.get("person") or "").strip()
    people: dict[str, dict] = {}
    for m in range(1, 13):
        month = f"{year}-{m:02d}"
        if month > t._today().strftime("%Y-%m"):
            break
        for p in _month_report(t, {"month": month, "person": who})["people"]:
            if not p["lines"] and not p["drives"] and not p["bank"]:
                continue
            me = people.setdefault(p["person"], {"person": p["person"], "months": [], "cats": {}})
            me["months"].append(
                {
                    "month": month,
                    "receipts": p["receipts"],
                    "bank_lines": len(p["bank"]),
                    "total": p["total"],
                    "waiting": p["waiting_on_a_rate"] + p["bank_waiting"],
                    "review": p["review"],
                }
            )
            for line in p["lines"]:
                if line["dollars"] is not None:
                    cents = int(Decimal(line["dollars"]) * 100)
                    me["cats"][line["category"]] = me["cats"].get(line["category"], 0) + cents
    dated = {str(e.get("sha256")) for _i, e in _events(t, DATED_EVENT)}
    filed = {
        str(r["receipt_sha256"])
        for r in t._rows("SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,))
    }
    for _i, e in _events(t, LANDED_EVENT):
        person, sha = str(e.get("person", "")), str(e.get("sha256", ""))
        if records.UNDATED not in str(e.get("file", "")) or sha in dated | filed:
            continue
        if (who and who.casefold() not in person.casefold()) or not t.sees(
            "expense.report", unit_of(person)
        ):
            continue
        me = people.setdefault(person, {"person": person, "months": [], "cats": {}})
        me["undated"] = me.get("undated", 0) + 1
    out = []
    for me in sorted(people.values(), key=lambda p: p["person"]):
        out.append(
            {
                "person": me["person"],
                "months": me["months"],
                "by_category": {k: money(v) for k, v in sorted(me["cats"].items())},
                "not_yet_in_a_month": me.get("undated", 0),
            }
        )
    return {"year": year, "people": out}


def _start_here(t: Tools, args: dict) -> dict:
    """Who the person is and Tim's answers for the tools they have (the 10/14
    phone run: with no way to ask, Claude guessed who the reviewer was)."""
    from ..capabilities.faq import faq_for

    answers = faq_for(t._specs)
    if t.viewer is None:
        return {"you": {"ministry": t.ministry}, "answers": answers}
    policy, me = t.viewer.policy, t.viewer.person

    def files(who: str) -> bool:
        return evaluate(policy, Request(who, "submit", "expense.report", scope=who)).allowed

    def looks_over(who: str, whom: str) -> bool:
        return any(
            evaluate(policy, Request(who, action, "expense.report", scope=whom)).allowed
            for action in _PLACERS
        )

    others = [p for p in policy.people if p != me]
    held = policy.people[me].roles if me in policy.people else ()
    return {
        "you": {
            "name": display_name(me),
            "does": list(held),
            "ministry": t.ministry,
            "sends_receipts": bool(t.receipts is not None and t.receipts.offered()),
            "looks_over": [display_name(p) for p in others if files(p) and looks_over(me, p)],
            "looked_over_by": [display_name(p) for p in others if files(me) and looks_over(p, me)],
        },
        "answers": answers,
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
        "start_here",
        "Who the signed-in person is (their name, what they do, the ministry, whose spending "
        "they look over and who looks over theirs) and Tim's own answers to how-do-I "
        "questions, for the tools this person has. Call it at the start of a new chat and "
        "whenever the person asks who they are, how to do something with Tim, or what Tim "
        "can do, and answer from it in your own words. The first answer is the one setting "
        "that lets Tim work without asking each time: offer it to anyone whose app keeps "
        "asking.",
        _schema(),
        _start_here,
    ),
    ToolSpec(
        "deadlines",
        "Dated obligations (filings, renewals, reports) due within the next N days, "
        "from the tenant's obligations file, minus any marked done.",
        _schema({"days": {"type": "integer", "minimum": 0, "maximum": 3650}}),
        _deadlines,
    ),
    ToolSpec(
        "waiting_cards",
        "Approval cards still waiting for a yes or no. Speak of each by its about line: "
        "whose it is, what it's for, and how much. The card number is only for decide_card, "
        "never for the person.",
        _schema(_LIMIT),
        _waiting_cards,
    ),
    ToolSpec(
        "open_payables",
        "Vendor invoices not yet paid, earliest due first, with the count, the total owed, "
        "and a subtotal per vendor, all over every open bill even when the list is cut "
        "short. Quote those sums as given rather than adding amounts yourself. Each row's "
        "source is for your calls, never for the person's ears.",
        _schema(_LIMIT),
        _open_payables,
    ),
    ToolSpec(
        "find_invoices",
        "Find vendor invoices by vendor name or invoice number (any status), with the count, "
        "the total, and a subtotal per status (paid, unpaid, scheduled), all over every match "
        "even when the list is cut short. Quote those sums as given rather than adding "
        "amounts yourself. Each row's source is for your calls, "
        "never for the person's ears.",
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
        "Expense reports with their totals and where each stands: waiting for approval, "
        "approved (going into the books on the next run), open, reimbursed, or cleared. To "
        "approve or decline one, find it in waiting_cards and use decide_card.",
        _schema(_LIMIT),
        _expense_reports,
    ),
    ToolSpec(
        "month_report",
        "A person's month of receipts, for the person or the one who reviews them: each "
        "receipt's date and dollars, a receipt paid in another currency shown as the person "
        "said it beside its dollar value at the exchange rate on the receipt's own date, the "
        "month's dollar total, and where the review stands. Quote amounts and rates as given, "
        "never converting yourself; a receipt still waiting on its day's rate has no dollar "
        "value yet, so say so. Leave month out for this month (YYYY-MM otherwise); person "
        "narrows it to one name. To approve the month, find its card in waiting_cards. It "
        "also lists the person's drives (when, how far, and what each was for): when they ask "
        "about their drives, read them here and ask about each one still waiting on their word. "
        "Its bank lines are the month's statement lines and school payments: what each was "
        "(owed back, the ministry paid, sent abroad, paid back, came in) and what each still "
        "waits on. rules_say, when present, is what the ministry's own rules say about the "
        "month, each with the document and page it came from: tell the person plainly, as "
        "their ministry's rule, never as your judgment. Each line's source is for your calls; "
        "never say it to the person.",
        _schema(
            {
                "person": {"type": "string"},
                "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
            }
        ),
        _month_report,
    ),
    ToolSpec(
        "year_report",
        "Where a person's year stands, for the person or the one who reviews them: each "
        "month's receipts, dollar total, how many still wait on a day, an amount or a rate, "
        "and where its review stands; totals by category; and receipts sent in a catch-up "
        "that aren't in a month yet. Quote it as given. Leave year out for this year.",
        _schema(
            {
                "person": {"type": "string"},
                "year": {"type": "string", "pattern": "^\\d{4}$"},
            }
        ),
        _year_report,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(s.name for s in SPECS)


def _decide(t: Tools, args: dict) -> dict:
    return t.decider.decide(t, args)


ASK_THE_RULES = ToolSpec(
    "ask_the_rules",
    "What the ministry's own rules say before someone spends: say what it is (and how much, "
    "when they know). It answers in each rule's words with the document and page it came "
    "from, and says which ones apply at that amount, such as an OK needed before buying. "
    "Tell the person plainly, as their ministry's rule, never as your judgment; it's advice, "
    "and nothing is decided or held. When it asks which kind of spending it is, ask them. "
    "Offered only when the ministry has written rules down.",
    _schema(
        {
            "what": {"type": "string", "description": "What they want to buy, in their words."},
            "amount": {"type": "string", "description": "About how much, in dollars."},
            "kind": {"type": "string", "description": "The ministry's category, when known."},
        },
        required=["what"],
    ),
    _ask_the_rules,
)
"""Before the spend (chunk 5): offered beside the rest when the ministry has
a ``rules/`` folder."""


DECIDE = ToolSpec(
    "decide_card",
    "Approve or reject one waiting card as the signed-in person, only when they asked for "
    "it in their own words; pass those words, exactly as they said them, in said. At or "
    "under the ministry's dollar line, when their words name whose it is or how much, their "
    "word decides it and the reply says it's done. Otherwise the reply is a link: show its "
    "show line as given, link and all; they tap it and confirm with Face ID or a "
    "fingerprint, the tap finishes it, and their page tells them it's done. Asked again "
    "later, it says where things stand. "
    "Tell the person whose it is, what it's for, and how much, never the card number.",
    _schema(
        {
            "card": {"type": "integer", "minimum": 1},
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "said": {
                "type": "string",
                "description": "The person's own words asking for this, exactly as they "
                "said them; never your summary of them.",
            },
        },
        ["card", "decision", "said"],
    ),
    _decide,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)


def _set_category(t: Tools, args: dict) -> dict:
    return t.receipts.set_category(t, args)


SET_CATEGORY = ToolSpec(
    "set_category",
    "Put one receipt in the month under a category, when the person asks: their own "
    "receipts, or any receipt in a month they review. Find the receipt's line in "
    "month_report and pass its source as line, the category as they said it, and their "
    "words in said. Their word decides it (a category moves no money); a month waiting "
    "for its yes shows the change before anyone approves. A filed month can't change.",
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "category": {"type": "string"},
            "said": {
                "type": "string",
                "description": "The person's own words asking for the change.",
            },
        },
        ["line", "category", "said"],
    ),
    _set_category,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _set_drive_purpose(t: Tools, args: dict) -> dict:
    return t.receipts.set_drive_purpose(t, args)


SET_DRIVE_PURPOSE = ToolSpec(
    "set_drive_purpose",
    "Say what one drive was for, when the person tells you: find it in month_report's "
    'drives and pass its source as line, and their own words in said ("visiting the church '
    'in Pokhara", or "that was personal"). A ministry drive is priced at the rate on its '
    "own day; a personal one is kept and never priced. Never guess what a drive was for.",
    _schema(
        {
            "line": {"type": "string", "description": "The drive's source from month_report."},
            "said": {"type": "string", "description": "The person's own words about the drive."},
        },
        ["line", "said"],
    ),
    _set_drive_purpose,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _set_amount(t: Tools, args: dict) -> dict:
    return t.receipts.set_amount(t, args)


SET_AMOUNT = ToolSpec(
    "set_amount",
    "Say how much a receipt was, for one added without an amount: find its line in "
    "month_report and pass its source as line, and the person's own words in said. "
    "Their words set it: the amount they say, or their yes to what the photo reads "
    "(the line's read). Never pass an amount they didn't say or agree to.",
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "said": {
                "type": "string",
                "description": "The person's own words: the amount, or their yes to the reading.",
            },
        },
        ["line", "said"],
    ),
    _set_amount,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _add_receipt(t: Tools, args: dict) -> dict:
    return t.receipts.add(str(args.get("said") or ""))


ADD_RECEIPT = ToolSpec(
    "add_receipt",
    "Add one of the signed-in person's own receipts, when they say they have one; pass "
    "their words, exactly as they said them, in said: the amount and currency are read "
    "from those words, never from you. The reply is a link: show its show line as given; "
    "their tap opens their phone's camera and the photo goes in with this month's "
    "receipts. With no amount in their words the link still works; ask how much after the "
    "photo and pass their words to set_amount.",
    _schema(
        {
            "said": {
                "type": "string",
                "description": "The person's own words about the receipt, exactly as they "
                "said them; never your summary of them.",
            },
        },
        ["said"],
    ),
    _add_receipt,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)


def _catch_up_link(t: Tools, args: dict) -> dict:
    return t.receipts.catch_up(t, args)


CATCH_UP_LINK = ToolSpec(
    "catch_up_link",
    "One link for a whole pile of receipts, when someone is catching up (a month or a "
    "year of them: photos, PDFs, bank statements, a school list): the signed-in "
    "person's own, or pass person to get the link for someone whose month they review. "
    "Show its show line as given.",
    _schema(
        {
            "person": {
                "type": "string",
                "description": "Whose receipts, when it isn't the signed-in person's own.",
            },
        }
    ),
    _catch_up_link,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)


def _set_receipt_day(t: Tools, args: dict) -> dict:
    return t.receipts.set_day(t, args)


SET_RECEIPT_DAY = ToolSpec(
    "set_receipt_day",
    "Say which day a receipt is from, for one sent later (a catch-up): find its line in "
    "month_report and pass its source as line, the day as YYYY-MM-DD, and the person's own "
    "words in said. It moves the receipt to that day's month, priced at that day's rate. "
    "Never pass a day they didn't say or agree to.",
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "day": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "said": {"type": "string", "description": "The person's own words about the day."},
        },
        ["line", "day", "said"],
    ),
    _set_receipt_day,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _receipts_to_read(t: Tools, args: dict) -> dict:
    return t.receipts.to_read(t, args)


RECEIPTS_TO_READ = ToolSpec(
    "receipts_to_read",
    "The next receipts waiting to be read (sent in a catch-up, with no day or amount yet), "
    "as images after this answer: the signed-in person's own, or pass person for someone "
    "whose month they review. Read each image and pass what it shows to record_reading. "
    "Each receipt's line is for your calls; never say it to the person.",
    _schema(
        {
            "person": {"type": "string", "description": "Whose receipts, when not their own."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
        }
    ),
    _receipts_to_read,
)


def _record_reading(t: Tools, args: dict) -> dict:
    return t.receipts.record_reading(t, args)


RECORD_READING = ToolSpec(
    "record_reading",
    "Record what one receipt's image shows, after receipts_to_read: its line, the day "
    "(YYYY-MM-DD), the amount and currency code (USD, NPR...), who was paid (payee), and "
    "the category it reads as. Only what the receipt shows; leave a field out when it "
    "doesn't show it. This is a reading, never the person's word: the amount counts only "
    "once they agree to it (set_amount). The line is for your calls; never say it to the "
    "person.",
    _schema(
        {
            "line": {"type": "string", "description": "The receipt's line from receipts_to_read."},
            "day": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "amount": {"type": "string", "description": "The total paid, as printed."},
            "currency": {"type": "string", "description": "A currency code: USD, NPR, INR..."},
            "payee": {"type": "string", "description": "Who was paid, as printed."},
            "category": {"type": "string", "description": "What it reads as: Meals, Travel..."},
        },
        ["line", "day"],
    ),
    _record_reading,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _agree_to_readings(t: Tools, args: dict) -> dict:
    return t.receipts.agree(t, args)


AGREE_TO_READINGS = ToolSpec(
    "agree_to_readings",
    "The person's one yes to every reading left in a month, after you've walked them "
    "through what was read (month_report's read on each line) and made their "
    "corrections with set_amount, set_receipt_day and set_category. Pass the month and "
    "their own words in said. Only on their yes; never on your own.",
    _schema(
        {
            "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
            "said": {"type": "string", "description": "The person's own words of agreement."},
            "person": {"type": "string", "description": "Whose month, when not their own."},
        },
        ["month", "said"],
    ),
    _agree_to_readings,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _send_month_for_review(t: Tools, args: dict) -> dict:
    return t.receipts.send_month(t, args)


SEND_MONTH_FOR_REVIEW = ToolSpec(
    "send_month_for_review",
    "Send a month to the reviewer as one card listing every receipt and drive, when the "
    "person says it's ready. Nothing goes while a line still waits on a day, an amount, "
    "or a rate; the answer says what.",
    _schema(
        {
            "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
            "person": {"type": "string", "description": "Whose month, when not their own."},
        },
        ["month"],
    ),
    _send_month_for_review,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _bank_statement(t: Tools, args: dict) -> dict:
    return t.receipts.bank_statement(t, args)


BANK_STATEMENT = ToolSpec(
    "bank_statement",
    "Bank statements sent through a catch-up link (the CSV a bank's website exports). With "
    "nothing named: what waits for the person's word. With a statement and its account: "
    "ministry keeps every line of the ministry's own account; own shows the lines of the "
    "person's own account, each with what Tim thinks it looks like, for them to pick with "
    "keep_own_lines (only they may see their own account). Ask whose account it is; never "
    "guess. Ids and row numbers are for your calls; never say them to the person.",
    _schema(
        {
            "statement": {"type": "string", "description": "The statement's id, from this tool."},
            "account": {"type": "string", "enum": ["ministry", "own"]},
            "person": {"type": "string", "description": "Whose, when not their own."},
        }
    ),
    _bank_statement,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _keep_own_lines(t: Tools, args: dict) -> dict:
    return t.receipts.keep_own(t, args)


KEEP_OWN_LINES = ToolSpec(
    "keep_own_lines",
    "Keep the lines the person picked from their own account's statement, after "
    "bank_statement showed them: pass the statement, the rows they picked, and their own "
    "words in said. Only the picked lines are kept; the rest are gone from Tim. Never pick "
    "for them.",
    _schema(
        {
            "statement": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["statement", "rows", "said"],
    ),
    _keep_own_lines,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _keep_school_list(t: Tools, args: dict) -> dict:
    return t.receipts.keep_school(t, args)


KEEP_SCHOOL_LIST = ToolSpec(
    "keep_school_list",
    "Add the person's school list (scholarships and school fees, sent through a catch-up "
    "link): each payment becomes a line in its own month with no receipt needed. Pass the "
    "statement id bank_statement listed for it, and their words in said.",
    _schema(
        {
            "statement": {"type": "string"},
            "said": {"type": "string"},
            "person": {"type": "string", "description": "Whose, when not their own."},
        },
        ["statement"],
    ),
    _keep_school_list,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _explain_bank_line(t: Tools, args: dict) -> dict:
    return t.receipts.explain_line(t, args)


EXPLAIN_BANK_LINE = ToolSpec(
    "explain_bank_line",
    "Say what a bank line was for, in the person's own words, when it has no receipt "
    "(month_report's bank lines say which wait). Pass its source as line and their words in "
    "said. A receipt sent for it later finds its line by itself.",
    _schema(
        {
            "line": {"type": "string", "description": "The bank line's source from month_report."},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["line", "said"],
    ),
    _explain_bank_line,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)

STATEMENT_TOOLS = (BANK_STATEMENT, KEEP_OWN_LINES, KEEP_SCHOOL_LIST, EXPLAIN_BANK_LINE)
"""The catch-up's bank statements and school list (t-statements)."""

BOOKS_TOOLS = frozenset({"recent_runs", "close_status"})
"""Tools about the books as a whole (the runs, the month's lock): offered
only to a person who may view the books in every unit."""
