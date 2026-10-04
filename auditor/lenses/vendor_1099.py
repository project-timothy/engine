"""Lens 12 — vendor 1099 sync: the registry's 1099 picture and QBO's agree.

The January 1099-NEC filing runs through QBO's 1099 module, which only
sees vendors whose ``Vendor1099`` flag ("Track payments for 1099") is on.
The registry (``vendors.toml`` + the paid ledger) is where the taxpayer
picture actually lives: who is 1099-shaped, over the year's reporting
floor (a tax-year table, not a constant — see
``auditor/advisory/facts.py``'s ``form_1099_floor_cents``), W-9 on file,
and which classifications are exempt. The two drift silently (a
1099-due vendor with the QBO flag off is dropped at filing time; design doc
``docs/w9-1099-design.md`` section 5). This lens recomputes the join
nightly:

- a taxpayer due a 1099-NEC whose matched QBO vendor(s) carry the flag OFF
  is ``flag-missing`` (WARN, nags until the owner flips it in QBO);
- a taxpayer the registry classifies as exempt (S/C corp) whose QBO vendor
  carries the flag ON is ``flag-set-on-exempt`` (WARN, one side is wrong);
- a taxpayer due a 1099-NEC with no QBO vendor matching any registry name
  or ledger alias is ``qbo-vendor-unmatched`` (WARN, could not verify).

Scope note (probed live 2026-08-26): QBO's Vendor query returns no
``TaxIdentifier`` field at all, so TIN presence cannot be checked here;
that stays with QBO's own 1099 module at filing time. Read-only, findings
only — the flag flip is the owner's UI act (or a future gated write).
"""

from __future__ import annotations

from ..advisory.facts import load_registry, year_end_candidates
from ..clients.qbo import AuditorQboClient
from ..findings import Finding
from . import AuditContext

LENS = "vendor-1099"

_QBO_PATH = "Expenses > Vendors > (vendor) > Edit > Track payments for 1099"


def _norm(name: str) -> str:
    """Case, whitespace, and comma/period-insensitive name identity. Exact
    beyond that: the registry's ``ledger_aliases`` carry the spellings
    other systems use (the trailing ', Inc' shape), never a guess here."""
    return " ".join(name.casefold().replace(",", " ").replace(".", " ").split())


def _members(candidate: dict) -> list[str]:
    return [m.strip() for m in str(candidate.get("vendor", "")).split(" + ") if m.strip()]


def _names_for(candidate: dict, registry: dict[str, dict]) -> list[str]:
    names: list[str] = []
    for member in _members(candidate):
        names.append(member)
        entry = registry.get(member, {})
        names.extend(str(a) for a in (entry.get("ledger_aliases") or []))
    return names


def check(ctx: AuditContext, client: AuditorQboClient | None = None) -> list[Finding]:
    if not ctx.tenant.vendor_1099_enabled or not ctx.tenant.qbo_token_file:
        return []
    registry, canonical = load_registry(ctx.tenant.slug, ctx.tenants_dir)
    candidates = year_end_candidates(
        ctx.ledger, ctx.tenant.slug, registry, canonical, ctx.tenants_dir, ctx.now
    )
    if not candidates:
        return []

    client = client or AuditorQboClient(ctx.tenant.qbo_token_file)
    by_name: dict[str, list[dict]] = {}
    for vendor in client.fetch_vendors():
        by_name.setdefault(_norm(vendor["display_name"]), []).append(vendor)

    findings: list[Finding] = []
    for cand in candidates:
        subject = str(cand["vendor"])
        names = _names_for(cand, registry)
        matched: dict[str, dict] = {}
        for name in names:
            for vendor in by_name.get(_norm(name), []):
                matched[vendor["id"]] = vendor
        flagged = [v for v in matched.values() if v["vendor_1099"]]
        unflagged = [v for v in matched.values() if not v["vendor_1099"]]
        paid = f"${cand['paid_cents'] / 100:,.2f}"
        recipient = str(cand.get("tax_recipient") or "").strip()
        who = f"{subject} (1099 recipient {recipient})" if recipient else subject

        if cand.get("form_1099_due", True):
            if not matched:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=subject,
                        condition="qbo-vendor-unmatched",
                        severity="WARN",
                        detail=f"{who}: 1099-NEC due on {paid} paid, but no QBO vendor is "
                        f"named {', '.join(repr(n) for n in names)}; the 1099 flag could not "
                        "be checked — add the QBO display name as a ledger_alias in "
                        "vendors.toml or create the vendor",
                    )
                )
            elif not flagged:
                ids = ", ".join(f"'{v['display_name']}' (id {v['id']})" for v in unflagged)
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=subject,
                        condition="flag-missing",
                        severity="WARN",
                        detail=f"{who}: 1099-NEC due on {paid} paid, but QBO vendor {ids} has "
                        f"'Track payments for 1099' OFF; turn it on ({_QBO_PATH}) so the "
                        "January 1099 module includes the vendor",
                    )
                )
        elif flagged:
            ids = ", ".join(f"'{v['display_name']}' (id {v['id']})" for v in flagged)
            classification = str(cand.get("tax_classification") or "corporation")
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="flag-set-on-exempt",
                    severity="WARN",
                    detail=f"{subject}: the registry classifies this taxpayer as "
                    f"{classification} (no 1099-NEC due) but QBO vendor {ids} has 'Track "
                    f"payments for 1099' ON; one side is wrong — turn the flag off "
                    f"({_QBO_PATH}) or correct tax_classification from the W-9",
                )
            )
    return findings
