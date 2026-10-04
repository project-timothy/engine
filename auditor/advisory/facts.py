"""Deterministic advisory facts, computed from ground truths.

Every number the advisory voice may speak is computed HERE, with the
auditor's own SQL over the ledger and its own parse of the vendor registry
(files as data, never engine code). Topics map one-to-one to what the
design names: coding drift, treatment questions, aging observations,
close-readiness, and the running year-end-CPA appendix.
"""

from __future__ import annotations

import hashlib
import tomllib
from datetime import datetime
from pathlib import Path

from ..config import default_tenants_dir
from ..ledger_reader import LedgerReader

# The auditor's own copies of the lifecycle groups (see lenses).
OPEN_STATUSES = ("Received", "Approved", "Outstanding", "Scheduled", "Scheduled in bill pay")
COMMITTED_STATUSES = ("Scheduled", "Scheduled in bill pay")
# Cost types whose vendors are 1099-shaped when paid enough in a year.
FORM_1099_COST_TYPES = frozenset({"freelancers", "subcontractors", "reimbursement"})
# The 1099-NEC/1099-MISC reporting floor is a LAW fact, not a tenant
# preference (owner catch 2026-09-28, issue #371): the One Big Beautiful
# Bill Act raised it from $600 to $2,000 for payments made after
# 2025-12-31, inflation-indexed from 2027 (not yet published). Keyed by
# the calendar year a payment lands in, each bracket applying from its
# year forward until superseded — never a single constant again.
FORM_1099_FLOOR_SCHEDULE: dict[int, int] = {
    2025: 60_000,  # $600 — 2025 and every year before it
    2026: 200_000,  # $2,000 — One Big Beautiful Bill Act
}
_FORM_1099_LATEST_KNOWN_YEAR = max(FORM_1099_FLOOR_SCHEDULE)
AGING_BUCKETS = ((0, 30), (31, 60), (61, 90), (91, 100_000))


def form_1099_floor_cents(tax_year: int) -> tuple[int, bool]:
    """(floor_cents, is_fallback) for the 1099-NEC/1099-MISC reporting floor
    in effect for ``tax_year``. A year past the latest published bracket
    (2027+, until the IRS publishes its inflation-indexed figure) falls
    back to the latest known year's floor, flagged so the report can say
    so; every year at or before the latest known bracket uses the settled
    figure for the bracket it falls into — not a fallback."""
    if tax_year > _FORM_1099_LATEST_KNOWN_YEAR:
        return FORM_1099_FLOOR_SCHEDULE[_FORM_1099_LATEST_KNOWN_YEAR], True
    applicable = [year for year in FORM_1099_FLOOR_SCHEDULE if year <= tax_year]
    bracket = max(applicable) if applicable else min(FORM_1099_FLOOR_SCHEDULE)
    return FORM_1099_FLOOR_SCHEDULE[bracket], False


def _load_registry(
    slug: str, tenants_dir: str | Path | None
) -> tuple[dict[str, dict], dict[str, str]]:
    """(canonical vendor -> entry, any ledger name -> canonical vendor).

    ``ledger_aliases`` fold historical row spellings into the canonical entry
    so a vendor rename (2026-07-28) never silently drops
    the taxpayer from the CPA appendix."""
    root = Path(tenants_dir) if tenants_dir else default_tenants_dir()
    path = root / slug / "vendors.toml"
    if not path.exists():
        return {}, {}
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    registry: dict[str, dict] = {}
    canonical: dict[str, str] = {}
    for entry in raw.values():
        if isinstance(entry, dict) and entry.get("vendor"):
            name = str(entry["vendor"])
            registry[name] = entry
            canonical[name] = name
            for alias in entry.get("ledger_aliases", []) or []:
                canonical[str(alias)] = name
    return registry, canonical


def _age_days(date_text: str, now: datetime) -> int | None:
    if not date_text:
        return None
    try:
        then = datetime.fromisoformat(str(date_text)[:10] + "T00:00:00+00:00")
    except ValueError:
        return None
    return max(0, int((now - then).total_seconds() // 86400))


def _coding_drift(ledger: LedgerReader, slug: str) -> dict:
    """Vendors coded to more than one account this year."""
    rows = ledger.query(
        "SELECT vendor, gl_account, COUNT(*) AS n, SUM(amount_cents) AS cents "
        "FROM ap_invoices WHERE tenant = ? AND gl_account != '' "
        "GROUP BY vendor, gl_account",
        (slug,),
    )
    by_vendor: dict[str, list[dict]] = {}
    for row in rows:
        by_vendor.setdefault(row["vendor"], []).append(row)
    drifted = {}
    for vendor, codings in sorted(by_vendor.items()):
        if len(codings) > 1:
            drifted[vendor] = [
                {"account": c["gl_account"], "rows": c["n"], "cents": c["cents"]} for c in codings
            ]
    return {"vendors_coded_multiple_ways": drifted}


def _aging(ledger: LedgerReader, slug: str, now: datetime) -> dict:
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    rows = ledger.query(
        f"SELECT vendor, invoice_number, invoice_date, amount_cents, status "
        f"FROM ap_invoices WHERE tenant = ? AND status IN ({placeholders})",
        (slug, *OPEN_STATUSES),
    )
    buckets = {
        f"{lo}-{hi if hi < 1000 else 'plus'}": {"count": 0, "cents": 0} for lo, hi in AGING_BUCKETS
    }
    oldest: dict | None = None
    for row in rows:
        age = _age_days(row["invoice_date"], now)
        if age is None:
            continue
        for lo, hi in AGING_BUCKETS:
            if lo <= age <= hi:
                key = f"{lo}-{hi if hi < 1000 else 'plus'}"
                buckets[key]["count"] += 1
                buckets[key]["cents"] += row["amount_cents"]
                break
        if oldest is None or age > oldest["age_days"]:
            oldest = {
                "vendor": row["vendor"],
                "invoice_number": row["invoice_number"],
                "age_days": age,
                "cents": row["amount_cents"],
            }
    return {"open_payables": len(rows), "buckets": buckets, "oldest": oldest}


def _close_readiness(ledger: LedgerReader, slug: str, now: datetime) -> dict:
    committed_placeholders = ",".join("?" for _ in COMMITTED_STATUSES)
    committed = ledger.query(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents FROM ap_invoices "
        f"WHERE tenant = ? AND status IN ({committed_placeholders})",
        (slug, *COMMITTED_STATUSES),
    )[0]
    last_settle = ""
    for event in ledger.events():
        if event.get("event_type") == "ap.reconcile.paid":
            last_settle = max(last_settle, str(event.get("created_at", "")))
    return {
        "committed_not_cleared": {"count": committed["n"], "cents": committed["cents"]},
        "last_reconcile_settle": last_settle[:10],
        "as_of": now.date().isoformat(),
    }


def _treatment_questions(
    ledger: LedgerReader, slug: str, registry: dict, canonical: dict[str, str]
) -> dict:
    """Reimbursement-shaped vendors and their year totals — the standing
    owner-reimbursement vs loans-payable treatment question, kept in view."""
    reimbursement_vendors = sorted(
        name
        for name, entry in registry.items()
        if str(entry.get("cost_type", "")).lower() == "reimbursement"
    )
    totals = []
    for name in reimbursement_vendors:
        row_names = sorted(alias for alias, canon in canonical.items() if canon == name)
        placeholders = ",".join("?" for _ in row_names)
        rows = ledger.query(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents "
            f"FROM ap_invoices WHERE tenant = ? AND vendor IN ({placeholders})",
            (slug, *row_names),
        )
        if rows[0]["n"]:
            totals.append({"vendor": name, "rows": rows[0]["n"], "cents": rows[0]["cents"]})
    return {"reimbursement_vendors": totals}


# tax_classification values that exempt a taxpayer from 1099-NEC reporting.
# Empty stays reportable — that is the field's contract, never a guess.
_CORP_CLASSIFICATIONS = frozenset({"s-corp", "c-corp", "corporation"})


def _owner_names(slug: str, tenants_dir: str | Path | None) -> frozenset[str]:
    """The tenant's owners ([close].owner_names, casefolded). Owner
    reimbursements and loan repayments in the AP book are equity/liability
    traffic, never 1099 income (the 2026-07-11 owner delineation)."""
    root = Path(tenants_dir) if tenants_dir else default_tenants_dir()
    path = root / slug / "tenant.toml"
    if not path.exists():
        return frozenset()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    names = (raw.get("close") or {}).get("owner_names") or []
    return frozenset(str(n).strip().casefold() for n in names if str(n).strip())


def tax_year_for(now: datetime) -> int:
    """The 1099 tax year the running appendix reports: the calendar year of
    ``now``, except January, when the prior year's packet is what the CPA
    is assembling (payments are cash-basis: the year the check was cut)."""
    return now.year - 1 if now.month == 1 else now.year


def _year_end_cpa(
    ledger: LedgerReader,
    slug: str,
    registry: dict,
    canonical: dict[str, str],
    owners: frozenset[str] = frozenset(),
    tax_year: int | None = None,
) -> dict:
    """The running January-handoff appendix, counted per TAXPAYER.

    Rows fold to the canonical registry name via ``ledger_aliases``; canonical
    vendors sharing a declared ``tax_entity`` then fold to one taxpayer unit
    (a vendor with no entity stands alone). Candidate flagging, the
    tax-year floor (issue #371), and W-9 coverage all test the taxpayer,
    not the vendor row — the shared-EIN false-positive fix (design
    2026-07-28, defects 2 and 3). Corporations stay listed with the
    exemption stated, never silently dropped, and a declared
    ``tax_recipient`` (the 1099 line-1 name) rides along for disregarded
    SMLLCs."""
    # The floor is a law fact keyed by the year this run is reporting (the
    # whole appendix is scoped to one tax year via ``year_clause`` below,
    # so every payment it counts already shares that year — "judged by
    # the year it was paid" reduces to one lookup per run, not per row).
    # No tax_year given falls back to the latest known bracket.
    floor_cents, floor_is_fallback = form_1099_floor_cents(
        tax_year if tax_year is not None else _FORM_1099_LATEST_KNOWN_YEAR
    )
    # The ledger row's own cost_type is 1099 evidence too (2026-08-26, the
    # unregistered-subcontractor miss): a vendor paid as Subcontractors with
    # no vendors.toml entry must still surface, flagged as unregistered.
    # Cash basis: a payment counts in the year it was made (payment_date,
    # falling back to the invoice date; a row dated neither way still counts
    # rather than silently dropping money). Before 2026-08-26 every Paid row
    # counted forever, which would have carried 2026 payments into the 2027
    # appendix at the rollover.
    year_clause = ""
    params: tuple = (slug,)
    if tax_year is not None:
        year_clause = (
            " AND (COALESCE(NULLIF(payment_date, ''), NULLIF(invoice_date, '')) IS NULL "
            "OR substr(COALESCE(NULLIF(payment_date, ''), invoice_date), 1, 4) = ?)"
        )
        params = (slug, str(tax_year))
    rows = ledger.query(
        "SELECT vendor, COALESCE(cost_type, '') AS cost_type, "
        "COALESCE(SUM(amount_cents), 0) AS cents, COUNT(*) AS n "
        f"FROM ap_invoices WHERE tenant = ? AND status = 'Paid'{year_clause} "
        "GROUP BY vendor, cost_type ORDER BY vendor",
        params,
    )
    # Vendor-role expense payments (issue #120) are payments to a taxpayer
    # too: recorded/cleared reports count, Open does not. The person name
    # folds to the canonical vendor via ledger_aliases exactly like an AP
    # row spelling; a person with no registry entry (an owner's
    # reimbursements) never becomes a candidate because nothing marks the
    # unit 1099-shaped. Pre-expenses ledgers have no expense tables — that
    # reads as zero expense payments. With the table PRESENT a failing
    # query propagates to the runner's "advisory unavailable this night"
    # line (honesty audit 2026-09-03, 04-F7): a schema drift must never
    # silently drop every vendor-role reimbursement from the total.
    has_expense_table = bool(
        ledger.query("SELECT name FROM sqlite_master WHERE type='table' AND name='expense_report'")
    )
    if has_expense_table:
        report_year = ""
        report_params: tuple = (slug,)
        if tax_year is not None:
            report_year = (
                " AND substr(COALESCE(NULLIF(reimbursed_date, ''), month || '-01'), 1, 4) = ?"
            )
            report_params = (slug, str(tax_year))
        rows += ledger.query(
            "SELECT person AS vendor, '' AS cost_type, "
            "COALESCE(SUM(total_cents), 0) AS cents, COUNT(*) AS n "
            "FROM expense_report WHERE tenant = ? "
            f"AND status IN ('Reimbursed-Recorded', 'Reimbursed'){report_year} "
            "GROUP BY person ORDER BY person",
            report_params,
        )
    # fold alias-named rows into their canonical taxpayer before any flagging
    merged: dict[str, dict] = {}
    for row in rows:
        name = canonical.get(row["vendor"], row["vendor"])
        if name.strip().casefold() in owners:
            continue  # owners are never 1099 taxpayers of their own company
        bucket = merged.setdefault(name, {"cents": 0, "n": 0, "cost_types": set()})
        bucket["cents"] += row["cents"]
        bucket["n"] += row["n"]
        if str(row.get("cost_type") or "").strip():
            bucket["cost_types"].add(str(row["cost_type"]).strip())
    # taxpayer units: one per declared tax_entity, else one per vendor.
    # Members append in name order, so a unit's display join is stable.
    units: dict[str, dict] = {}
    for name in sorted(merged):
        entity = str(registry.get(name, {}).get("tax_entity", "") or "")
        unit = units.setdefault(
            entity or f"vendor::{name}",
            {"members": [], "entity": entity, "cents": 0, "n": 0},
        )
        unit["members"].append(name)
        unit["cents"] += merged[name]["cents"]
        unit["n"] += merged[name]["n"]
    flagged = []
    for unit in sorted(units.values(), key=lambda u: u["members"][0]):
        entries = [registry.get(name, {}) for name in unit["members"]]
        registered = any(name in registry for name in unit["members"])
        ledger_types = set()
        for name in unit["members"]:
            ledger_types |= merged[name]["cost_types"]
        shaped = any(
            str(e.get("cost_type", "")).lower() in FORM_1099_COST_TYPES for e in entries
        ) or any(t.lower() in FORM_1099_COST_TYPES for t in ledger_types)
        if not shaped or unit["cents"] < floor_cents:
            continue
        # any member's W-9 covers the taxpayer (one EIN, one form); a declared
        # false beats silence; nothing declared anywhere stays unknown.
        w9_states = [e.get("w9") for e in entries]
        if any(bool(state) for state in w9_states):
            w9: bool | str = True
        elif any(state is not None for state in w9_states):
            w9 = False
        else:
            w9 = "unknown"
        classification = next(
            (c for e in entries if (c := str(e.get("tax_classification", "")).strip())), ""
        )
        recipient = next((r for e in entries if (r := str(e.get("tax_recipient", "")).strip())), "")
        cost_types = sorted(
            {str(e.get("cost_type", "")) for e in entries if e.get("cost_type")} | ledger_types
        )
        # Reportability ships POSITIVELY and in words (2026-09-21): this was
        # `no_1099_due`, and on 09-20 and 09-21 the drafted advisory asserted
        # the exact inverse of it for every taxpayer it named. A key spelled
        # as a negation is one dropped word away from its opposite once prose
        # paraphrases it, and this is the one fact in the appendix that tells
        # the owner to act or not to act before January.
        exempt = classification.replace(" ", "-").lower() in _CORP_CLASSIFICATIONS
        flagged.append(
            {
                "vendor": " + ".join(unit["members"]),
                "registered": registered,
                "tax_entity": unit["entity"],
                "cost_type": " / ".join(cost_types),
                "paid_cents": unit["cents"],
                "payments": unit["n"],
                "w9_on_file": w9,
                "tax_classification": classification,
                "form_1099_due": not exempt,
                "form_1099_note": (
                    f"exempt ({classification or 'corporation'}): no 1099-NEC due"
                    if exempt
                    else "1099-NEC due"
                ),
                "tax_recipient": recipient,
            }
        )
    # tax_entity groups: distinct vendors billing under one EIN sum together
    groups: dict[str, int] = {}
    for name, bucket in merged.items():
        entity = str(registry.get(name, {}).get("tax_entity", "") or "")
        if entity:
            groups[entity] = groups.get(entity, 0) + bucket["cents"]
    return {
        "form_1099_candidates": flagged,
        "tax_entity_paid_totals": groups,
        "floor_cents": floor_cents,
        "floor_is_fallback": floor_is_fallback,
        "tax_year": tax_year,
    }


def load_registry(
    slug: str, tenants_dir: str | Path | None
) -> tuple[dict[str, dict], dict[str, str]]:
    """Public face of the registry reader for lenses (vendor-1099)."""
    return _load_registry(slug, tenants_dir)


def year_end_candidates(
    ledger: LedgerReader,
    slug: str,
    registry: dict,
    canonical: dict[str, str],
    tenants_dir: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The 1099 candidate units exactly as the CPA appendix counts them —
    one computation, shared by the advisory and the vendor-1099 lens."""
    owners = _owner_names(slug, tenants_dir)
    year = tax_year_for(now) if now is not None else None
    cpa = _year_end_cpa(ledger, slug, registry, canonical, owners, year)
    return list(cpa["form_1099_candidates"])


def w9_gap(candidate: dict) -> bool:
    """A W-9 gap worth a nightly nag: a taxpayer DUE a 1099-NEC with no
    W-9 on file. An exempt corporation without a form is recorded (the
    classification is the owner's attestation until a form arrives) but
    does not nag — owner decision 2026-08-26.

    A candidate with no reportability field at all defaults to DUE, which is
    the same contract an empty ``tax_classification`` carries: unknown stays
    reportable, never silently exempt."""
    return candidate.get("w9_on_file") is not True and bool(candidate.get("form_1099_due", True))


def w9_exempt_without_form(candidate: dict) -> bool:
    """The other half of the W-9 picture: a taxpayer the owner classified as
    EXEMPT who still has no form on file.

    Not a nag (2026-08-26) and not a gap -- but not nothing either, which is
    what the unchanged-night summary used to imply by reporting an empty nag
    list as "all W-9s on file". The two predicates partition the taxpayers
    with no form between them, so the summary can state the one it measured
    and count the other."""
    return candidate.get("w9_on_file") is not True and not bool(
        candidate.get("form_1099_due", True)
    )


# The auditor-store key holding the last-rendered appendix fingerprint.
CPA_APPENDIX_STATE_KEY = "cpa-appendix-fingerprint"


def cpa_appendix_fingerprint(cpa: dict) -> str:
    """Structural identity of the 1099 appendix: who is flagged, their W-9
    state, classification, and 1099 recipient — never amounts or payment
    counts, so routine payments to a settled taxpayer leave the appendix
    quiet (owner decision 2026-08-22: reprint on change, nag only the
    uncleared)."""
    items = sorted(
        (
            str(c.get("vendor", "")),
            str(c.get("w9_on_file")),
            str(c.get("registered", True)),
            str(c.get("tax_classification", "")),
            str(c.get("form_1099_due")),
            str(c.get("tax_recipient", "")),
        )
        for c in cpa.get("form_1099_candidates", [])
    )
    return hashlib.sha256(repr(items).encode()).hexdigest()[:16]


def _apply_acknowledgments(topics: dict[str, dict], acknowledged: dict[str, str]) -> None:
    """Drop owner-answered subjects from subject-shaped topics, in place.

    Keys are ``topic/subject`` (triage.toml ``[advisory.acknowledged]``). The
    voice drafts only from facts, so a dropped subject can never be re-asked.
    Unknown topics, unknown subjects, and malformed keys are ignored — an
    acknowledgment can never break the advisory."""
    for key in acknowledged:
        topic, _, subject = key.partition("/")
        data = topics.get(topic)
        if not (isinstance(data, dict) and subject):
            continue
        if topic == "coding-drift":
            data.get("vendors_coded_multiple_ways", {}).pop(subject, None)
        elif topic == "treatment-questions":
            data["reimbursement_vendors"] = [
                v for v in data.get("reimbursement_vendors", []) if v["vendor"] != subject
            ]


def compute_facts(
    ledger: LedgerReader,
    *,
    slug: str,
    now: datetime,
    tenants_dir: str | Path | None = None,
    muted_topics: frozenset[str] = frozenset(),
    acknowledged: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Every advisory topic's facts, minus what the owner has muted or
    already answered."""
    registry, canonical = _load_registry(slug, tenants_dir)
    owners = _owner_names(slug, tenants_dir)
    topics = {
        "coding-drift": lambda: _coding_drift(ledger, slug),
        "aging": lambda: _aging(ledger, slug, now),
        "close-readiness": lambda: _close_readiness(ledger, slug, now),
        "treatment-questions": lambda: _treatment_questions(ledger, slug, registry, canonical),
        "year-end-cpa": lambda: _year_end_cpa(
            ledger, slug, registry, canonical, owners, tax_year_for(now)
        ),
    }
    computed = {name: build() for name, build in topics.items() if name not in muted_topics}
    _apply_acknowledgments(computed, acknowledged or {})
    return computed
