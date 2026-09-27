"""Lens 7 — QBO consistency: the accounting system agrees with the ledger.

Three checks, drifting in either direction is a finding:

- LOCAL: every ``ap.qbo.bill_created`` event's claim matches its row (the
  row exists and carries the event's bill id) — the engine's write-side
  events and the book must tell one story;
- EXTERNAL: every stored ``qbo_bill_id`` still exists in QBO with the
  row's amount (integer cents; a vanished or edited bill is money-shaped
  drift, CRITICAL);
- EXTERNAL: every reconcile-flipped row's evidence transaction still
  exists in QBO (the payment that justified a Paid flip cannot quietly
  disappear). Evidence amounts are compared as WARN — a payment may
  legitimately bundle more than one invoice.

The external reads use the auditor's own client (queries only) with the
shared token-file lock honored; evals inject a fake client and never open
a socket.
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..clients.qbo import AuditorQboClient
from ..findings import Finding
from . import AuditContext

LENS = "qbo"

BILL_CREATED_EVENT = "ap.qbo.bill_created"
RECONCILE_PAID_EVENT = "ap.reconcile.paid"


def _events(ctx: AuditContext, event_type: str) -> list[dict]:
    records = []
    for row in ctx.ledger.query(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ? ORDER BY id",
        (ctx.tenant.slug, event_type),
    ):
        try:
            records.append(json.loads(row["payload_json"]))
        except json.JSONDecodeError:
            continue
    return records


def check_write_records(ctx: AuditContext) -> list[Finding]:
    """The engine's bill_created events versus the rows they describe."""
    findings: list[Finding] = []
    for payload in _events(ctx, BILL_CREATED_EVENT):
        invoice_id = payload.get("invoice_id")
        claimed_bill = str(payload.get("qbo_bill_id", "") or "")
        if invoice_id is None or not claimed_bill:
            continue
        rows = ctx.ledger.query(
            "SELECT id, vendor, invoice_number, qbo_bill_id FROM ap_invoices WHERE id = ?",
            (int(invoice_id),),
        )
        if not rows:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=f"bill_created event for invoice #{invoice_id}",
                    condition="row-vanished",
                    severity="CRITICAL",
                    detail=f"an event records bill {claimed_bill} created for invoice "
                    f"#{invoice_id}, but no such row exists in the ledger",
                )
            )
            continue
        row = rows[0]
        if str(row["qbo_bill_id"] or "") != claimed_bill:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=f"invoice #{row['id']} {row['vendor']} / {row['invoice_number']}",
                    condition="write-record-mismatch",
                    severity="CRITICAL",
                    detail=f"the write event says bill {claimed_bill} was created for this "
                    f"row, but the row carries {row['qbo_bill_id'] or '(nothing)'}; the "
                    "events and the book tell different stories",
                )
            )
    return findings


def check_bills(ctx: AuditContext, client: AuditorQboClient) -> list[Finding]:
    rows = ctx.ledger.query(
        "SELECT id, vendor, invoice_number, amount_cents, qbo_bill_id FROM ap_invoices "
        "WHERE tenant = ? AND qbo_bill_id IS NOT NULL AND qbo_bill_id != '' ORDER BY id",
        (ctx.tenant.slug,),
    )
    if not rows:
        return []
    found = client.fetch_by_ids("Bill", [str(r["qbo_bill_id"]) for r in rows])
    findings: list[Finding] = []
    for row in rows:
        subject = f"invoice #{row['id']} {row['vendor']} / {row['invoice_number']}"
        bill_id = str(row["qbo_bill_id"])
        if bill_id not in found:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="bill-vanished",
                    severity="CRITICAL",
                    detail=f"the row carries bill id {bill_id} but the accounting system "
                    "no longer holds that bill",
                )
            )
        elif found[bill_id] != row["amount_cents"]:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="bill-amount-drift",
                    severity="CRITICAL",
                    detail=f"bill {bill_id} shows {found[bill_id]} cents in the accounting "
                    f"system but the ledger row says {row['amount_cents']} cents; someone "
                    "edited one side",
                )
            )
    return findings


def check_reconcile_evidence(ctx: AuditContext, client: AuditorQboClient) -> list[Finding]:
    by_entity: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for payload in _events(ctx, RECONCILE_PAID_EVENT):
        qbo_id = str(payload.get("qbo_id", "") or "")
        if ":" not in qbo_id:
            continue
        entity, raw_id = qbo_id.split(":", 1)
        by_entity[entity][raw_id].append(payload)

    findings: list[Finding] = []
    for entity, ids in sorted(by_entity.items()):
        found = client.fetch_by_ids(entity, list(ids))
        for raw_id, payloads in sorted(ids.items()):
            claimed = sum(int(p.get("amount_cents", 0) or 0) for p in payloads)
            subjects = ", ".join(
                f"#{p.get('invoice_id')} {p.get('vendor', '')}".strip() for p in payloads
            )
            if raw_id not in found:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=f"evidence {entity}:{raw_id}",
                        condition="evidence-vanished",
                        severity="CRITICAL",
                        detail=f"the payment that justified marking {subjects} Paid no "
                        "longer exists in the accounting system",
                    )
                )
            elif found[raw_id] < claimed:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=f"evidence {entity}:{raw_id}",
                        condition="evidence-amount-drift",
                        severity="WARN",
                        detail=f"this payment now shows {found[raw_id]} cents but it "
                        f"settled {claimed} cents across {subjects}; it shrank after "
                        "it was used as clearing evidence",
                    )
                )
    return findings


def check(ctx: AuditContext, client: AuditorQboClient | None = None) -> list[Finding]:
    findings = check_write_records(ctx)
    if not ctx.tenant.qbo_token_file:
        return findings  # tenant has no accounting-system connection
    client = client or AuditorQboClient(ctx.tenant.qbo_token_file)
    findings.extend(check_bills(ctx, client))
    findings.extend(check_reconcile_evidence(ctx, client))
    return findings
