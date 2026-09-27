"""Lens 13 — materiality outlier: an amount out of line with its vendor.

Re-founded on the ledger from a rule the engine's predecessor carried.
Nothing else in the engine looks at amount plausibility: the approval card
puts every bill in front of the owner, but a mis-extracted amount approved
in a batch (an OCR that drops a decimal point) posts to the accounting
system and sits until the bank feed disagrees. A row
fires when ALL of:

1. the amount is over the tenant's floor ($5,000 by default, to revisit once a
   tenant has six months of ledger data);
2. the row carries an invoice date (the window is relative to the row's
   OWN date so the verdict is the same on a historical pass);
3. the vendor has at least one earlier row inside the trailing window
   (365 days by default);
4. the amount exceeds the MEDIAN of those peers times the multiple (3x by
   default). Median, not mean, so one prior outlier cannot lift the floor.

Void and cancelled rows neither fire nor count as peers. The subject is
(vendor, invoice number, amount to the cent), never the row id, so the
fingerprint survives re-runs and a dated mute on it never suppresses a
different amount under the same number. Acknowledge a confirmed amount in
triage.toml: ``muted = [{key = "<fingerprint>", since = "YYYY-MM-DD",
why = "confirmed against the source document"}]`` (the fingerprint is in
the detail line; the dated mute ages out after 90 days by design).
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from ..findings import Finding
from . import AuditContext
from ._tables import dollars, has_table

LENS = "materiality"

_INERT_STATUSES = ("Void%", "Cancelled")


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _vendor_key(vendor: str) -> str:
    return " ".join(str(vendor).casefold().split())


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.materiality_enabled or not has_table(ctx, "ap_invoices"):
        return []
    rows = ctx.ledger.query(
        "SELECT id, vendor, invoice_number, invoice_date, amount_cents FROM ap_invoices "
        "WHERE tenant = ? AND invoice_date != '' AND status NOT LIKE ? AND status != ? "
        "ORDER BY id",
        (ctx.tenant.slug, *_INERT_STATUSES),
    )
    dated = []
    for row in rows:
        when = _parse_date(row["invoice_date"])
        if when is not None:
            dated.append((row, when))
    by_vendor: dict[str, list[tuple[dict, date]]] = {}
    for row, when in dated:
        by_vendor.setdefault(_vendor_key(row["vendor"]), []).append((row, when))

    floor = ctx.tenant.materiality_floor_cents
    multiple = ctx.tenant.materiality_multiple
    window = timedelta(days=ctx.tenant.materiality_window_days)
    findings: list[Finding] = []
    for row, when in dated:
        amount = int(row["amount_cents"])
        if amount <= floor:
            continue
        peers = [
            int(peer["amount_cents"])
            for peer, peer_when in by_vendor[_vendor_key(row["vendor"])]
            if peer["id"] != row["id"] and when - window <= peer_when < when
        ]
        if not peers:
            continue
        typical = median(peers)
        if typical <= 0 or amount <= multiple * typical:
            continue
        subject = f"{row['vendor']} / {row['invoice_number'] or '(no number)'} / {dollars(amount)}"
        handle = Finding(
            lens=LENS, subject=subject, condition="amount-outlier", severity="WARN", detail=""
        ).fingerprint
        findings.append(
            Finding(
                lens=LENS,
                subject=subject,
                condition="amount-outlier",
                severity="WARN",
                detail=f"AP row #{row['id']} dated {when.isoformat()}: {dollars(amount)} is "
                f"{amount / typical:.1f}x this vendor's trailing-{window.days}-day median "
                f"{dollars(int(round(typical)))} ({len(peers)} peer row(s)); confirm the "
                "amount against the source document, then acknowledge with a dated mute "
                f"on fingerprint {handle}",
            )
        )
    return findings
