"""Lens 20 — sender provenance (#356 shadow stage): who mailed each invoice.

AP intake binds an invoice to a vendor by the name printed in it, and since
#356 records a verdict on the mail that delivered it (``ap.provenance.recorded``):
``match``, ``owner_drop``, ``internal_forward``, ``platform`` or ``mismatch``.
Nothing holds on a verdict yet. This lens is where the two that bind nothing
surface, INFO only, so a fortnight of real mail shows what enforcement would
hold and why before the owner turns it on:

- ``mismatch``: the sender is neither the vendor's domain, an address listed
  for it, nor the business itself. A look-alike domain lands here, and so
  does a vendor whose real address was never written down.
- ``platform``: an invoicing platform's shared mailer, which anyone with an
  account can send from under any company name.
"""

from __future__ import annotations

import json
from datetime import datetime

from ..findings import Finding
from . import AuditContext

LENS = "provenance"
EVENT = "ap.provenance.recorded"
# Long enough that a verdict stays on the checklist through a weekend, short
# enough that the report is about this fortnight's mail.
LOOKBACK_DAYS = 7
UNBOUND = {"mismatch": "sender-mismatch", "platform": "sender-platform"}


def _payload(row) -> dict:
    try:
        parsed = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def check(ctx: AuditContext) -> list[Finding]:
    findings: list[Finding] = []
    for row in ctx.ledger.query(
        "SELECT payload_json, created_at FROM events "
        "WHERE tenant = ? AND event_type = ? ORDER BY created_at",
        (ctx.tenant.slug, EVENT),
    ):
        age = (ctx.now - datetime.fromisoformat(row["created_at"])).total_seconds() / 86400
        if age > LOOKBACK_DAYS:
            continue
        p = _payload(row)
        condition = UNBOUND.get(str(p.get("verdict")))
        if condition is None:
            continue
        sender = p.get("sender") or p.get("sender_domain") or "an unknown sender"
        why = (
            "a shared invoicing mailer that binds no vendor"
            if condition == "sender-platform"
            else f"not the vendor's domain or a listed address ({p.get('reason') or 'domain'})"
        )
        findings.append(
            Finding(
                lens=LENS,
                subject=f"{p.get('vendor', '?')} / {p.get('file', '?')}",
                condition=condition,
                severity="INFO",
                detail=f"invoice naming {p.get('vendor', '?')} came from {sender}: {why}. "
                "Shadow stage, nothing held; a legitimate address belongs in that "
                "vendor's senders in vendors.toml",
            )
        )
    return findings
