"""Lens 15 — reconcile unknowns: money that left the bank and matched nothing.

The engine's reconcile records a cleared payment it cannot explain as an
``ap.reconcile.unknown`` event (no card, counted in the run summary) and
never mentions it again; the events age out of the query window in 30
days. Such a clearing is an unlogged
payment, not a search miss, and it deserves a surface the owner reads the
next morning. This lens lists every unknown clearing whose date falls
inside the window, once each, keyed by the bank-side transaction id (the
same key the engine's event carries), WARN. A clearing the engine logs
more than once reports its NEWEST event: the QBO record behind it can be
edited between runs (an amount trimmed after the first event was written),
and the oldest event then names a figure that never cleared. Payees the tenant tells the
engine to ignore (owner pay-outs) never produce the event, so they never
appear here. Read-only: recording the row, or deciding it is not AP,
stays the owner's act.

One clearing is not unknown money however loudly the event log says so: the
accounting-system Purchase the engine wrote for one of this ledger's own
EXPENSE REPORTS. An expense report commits money with no ``ap_invoices``
row, so before the engine learned that join every
report that ever cleared was logged unknown, and the log is append-only —
the engine's corrected verdict writes nothing, so the stale event would
re-ask the owner about their own recorded money for the rest of the window.
The lens re-derives the fact rather than trusting an engine verdict, which
is the whole point of auditing independently: ``qbo_purchase_id`` on
``expense_report`` is the exact id the engine recorded at ``expenses
match``, so that report IS the ledger row this clearing matched.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ..findings import Finding
from . import AuditContext
from ._tables import dollars, has_table

LENS = "reconcile"
EVENT = "ap.reconcile.unknown"


def _parse_date(text: object) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _expense_report_purchases(ctx: AuditContext) -> set[str]:
    """Raw Purchase ids this ledger's own expense reports were recorded as.

    ``qbo_purchase_id`` is written by the engine at ``expenses match``, the
    moment it creates the record, so the id is identity and not a heuristic:
    no payee, no amount, no date window. A report that never reached the
    accounting system carries none and excludes nothing.
    """
    if not has_table(ctx, "expense_report"):
        return set()
    return {
        str(row["qbo_purchase_id"])
        for row in ctx.ledger.query(
            "SELECT qbo_purchase_id FROM expense_report WHERE tenant = ? "
            "AND qbo_purchase_id IS NOT NULL AND qbo_purchase_id != ''",
            (ctx.tenant.slug,),
        )
    }


def _is_expense_report_purchase(qbo_id: str, recorded: set[str]) -> bool:
    """Entity and exact id, both. ``BillPayment:304`` is not ``Purchase:304``."""
    entity, _, ident = qbo_id.partition(":")
    return entity == "Purchase" and bool(ident) and ident in recorded


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.reconcile_enabled or not has_table(ctx, "events"):
        return []
    cutoff = ctx.now.date() - timedelta(days=ctx.tenant.reconcile_unknown_window_days)
    recorded_reports = _expense_report_purchases(ctx)
    # One entry per bank id: the first in-window sighting fixes the report
    # order, the newest one wins the numbers. The fingerprint is
    # lens + subject + condition, so refreshing the detail line carries every
    # mute and acknowledgment on the item forward untouched.
    latest: dict[str, tuple[date, dict[str, object]]] = {}
    for row in ctx.ledger.query(
        "SELECT payload_json, created_at FROM events WHERE tenant = ? AND event_type = ? "
        "ORDER BY id",
        (ctx.tenant.slug, EVENT),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        qbo_id = str(payload.get("qbo_id") or "")
        if not qbo_id:
            continue
        # The engine's own expense-report Purchase is recorded money by
        # construction (issue #281); the log just predates the rule.
        if _is_expense_report_purchase(qbo_id, recorded_reports):
            continue
        when = _parse_date(payload.get("date") or row["created_at"])
        if when is None or when < cutoff:
            continue
        latest[qbo_id] = (when, payload)
    findings: list[Finding] = []
    for qbo_id, (when, payload) in latest.items():
        amount = int(payload.get("amount_cents") or 0)
        payee = str(payload.get("payee") or "").strip() or "(no payee)"
        ref = str(payload.get("check_ref") or "").strip()
        instrument = f" (check {ref})" if ref else ""
        findings.append(
            Finding(
                lens=LENS,
                subject=qbo_id,
                condition="unknown-clearing",
                severity="WARN",
                detail=f"{dollars(amount)} to {payee} on {when.isoformat()}{instrument} "
                "cleared the bank and matched no ledger row (the engine's reconcile "
                "logged it as unknown); a hand-written check or an unlogged payment: "
                "record the row, or leave it if it is not AP (a pay-out, payroll, a "
                "card paydown)",
            )
        )
    return findings
