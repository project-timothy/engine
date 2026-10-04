"""Lens 5 — status coherence: every row's status agrees with its own story.

The status vocabulary below is the auditor's OWN copy of the AP lifecycle
(payable -> committed -> settled), deliberately duplicated from the fidelity
contract rather than imported: if the engine's vocabulary drifts, rows stop
matching and this lens says so — which is exactly the point.

Checks per row, all with the auditor's own SQL:

- the status string is in the known vocabulary;
- the current status equals the last history entry (when history exists);
- settled rows have no later flips (settled is terminal; corrections are a
  new row plus a void, never a silent reopen);
- every Paid row carries evidence: a payment date, a reconcile event, or an
  explicit owner flip (CRITICAL without one — that is money-shaped drift);
- payable rows carry no payment fields (a payment date or check ref on a
  Received row means money moved and the status never followed).
"""

from __future__ import annotations

import json

from ..findings import Finding
from . import AuditContext

LENS = "status"

# The auditor's own copy of the lifecycle (see module docstring).
PAYABLE = frozenset({"Received", "Approved", "Outstanding"})
COMMITTED = frozenset({"Scheduled", "Scheduled in bill pay"})
SETTLED = frozenset({"Paid", "Void - Already Paid", "Void - Duplicate", "Cancelled"})
KNOWN = PAYABLE | COMMITTED | SETTLED

RECONCILE_PAID_EVENT = "ap.reconcile.paid"


def _subject(row: dict) -> str:
    return f"invoice #{row['id']} {row['vendor']} / {row['invoice_number']}"


def _reconciled_invoice_ids(ctx: AuditContext) -> set[int]:
    ids: set[int] = set()
    for row in ctx.ledger.query(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant.slug, RECONCILE_PAID_EVENT),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        invoice_id = payload.get("invoice_id")
        if invoice_id is not None:
            ids.add(int(invoice_id))
    return ids


def check(ctx: AuditContext) -> list[Finding]:
    findings: list[Finding] = []
    rows = ctx.ledger.query(
        "SELECT * FROM ap_invoices WHERE tenant = ? ORDER BY id", (ctx.tenant.slug,)
    )
    reconciled = _reconciled_invoice_ids(ctx)

    for row in rows:
        subject = _subject(row)
        status = str(row["status"])

        if status not in KNOWN:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="unknown-status",
                    severity="WARN",
                    detail=f"status {status!r} is not in the AP lifecycle vocabulary",
                )
            )
            continue  # the remaining checks assume a known lifecycle position

        history = ctx.ledger.query(
            "SELECT * FROM ap_status_history WHERE invoice_id = ? ORDER BY id",
            (row["id"],),
        )
        if history and history[-1]["status_to"] != status:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="history-mismatch",
                    severity="WARN",
                    detail=f"row says {status!r} but the last recorded flip went to "
                    f"{history[-1]['status_to']!r}; the row and its history disagree",
                )
            )

        settled_seen = False
        for entry in history:
            if settled_seen:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=subject,
                        condition="settled-reopened",
                        severity="WARN",
                        detail=f"a flip to {entry['status_to']!r} was recorded after the row "
                        "settled; settled is terminal (corrections are a new row plus a void)",
                    )
                )
                break
            if entry["status_to"] in SETTLED:
                settled_seen = True

        if status == "Paid":
            owner_flip = any(
                h["status_to"] == "Paid" and (h["actor"] == "owner" or h["note"]) for h in history
            )
            if not row["payment_date"] and row["id"] not in reconciled and not owner_flip:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=subject,
                        condition="paid-without-evidence",
                        severity="CRITICAL",
                        detail="marked Paid with no payment date, no reconcile event, and "
                        "no owner flip; nothing shows this money actually cleared",
                    )
                )

        if status in PAYABLE and (row["payment_date"] or row["check_ref"]):
            field = "payment date" if row["payment_date"] else "check ref"
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="payable-with-payment-fields",
                    severity="WARN",
                    detail=f"status {status!r} says nothing has happened, but the row "
                    f"carries a {field}; if money moved, the status never followed",
                )
            )
    return findings
