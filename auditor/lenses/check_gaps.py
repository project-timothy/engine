"""Lens 14 — check-number sequence gaps: the hole a hand-written check leaves.

The old bookkeeper-auditor's A-BR-005, re-founded on the ledger
(2026-09-04). The engine only ever learns about a check that pays a ledger
row or clears the bank feed; a check the owner writes by hand for something
that never became an AP row (a profit share, hand-paid hours) touches
neither, and the only trace it leaves is a missing number in the paper
series. This lens rebuilds each series from the auditor's own reads:

- settled AP rows (a payment date and a check ref), every 3-to-5 digit run
  in the ref (``Check 1046``, ``CK1036``, ``3039+3040 (Jun 20)``); the
  lookarounds keep a 7-digit vendor id from reading as a check;
- expense reimbursements' ``instrument_ref`` (a reimbursement check);
- the bank's own sightings, ``ap.reconcile.unknown`` events carrying a
  check ref: a cleared-but-unrecorded number is OBSERVED here (the
  reconcile lens names it), never a gap twice.

A series (``1xxx``: the thousands band) needs ``min_observed`` numbers
before it is a sequence. Between two consecutive observed numbers, every
missing number is one ``check-gap`` finding per run of holes, WARN, when
the upper neighbour's date falls inside the window (90 days). Bank-assigned
electronic ids share the shape (a 9xxx series on some banks) and gap on
design; ``[auditor.check_gaps].series`` names the paper series to watch.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from ..findings import Finding
from . import AuditContext
from ._tables import has_table

LENS = "check-gaps"

_NUMBER = re.compile(r"(?<!\d)(\d{3,5})(?!\d)")


def numbers_in(ref: str | None) -> list[int]:
    return [int(m) for m in _NUMBER.findall(ref or "")]


def _series(n: int) -> str:
    return f"{n // 1000}xxx"


def _parse_date(text: object) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _observe(seen: dict[int, date], numbers: list[int], when: date | None) -> None:
    if when is None:
        return
    for n in numbers:
        held = seen.get(n)
        if held is None or when < held:
            seen[n] = when


def _observed(ctx: AuditContext) -> dict[int, date]:
    """Check number -> earliest date it was seen, across every source."""
    seen: dict[int, date] = {}
    slug = ctx.tenant.slug
    for row in ctx.ledger.query(
        "SELECT check_ref, payment_date FROM ap_invoices WHERE tenant = ? "
        "AND payment_date IS NOT NULL AND payment_date != '' AND check_ref != ''",
        (slug,),
    ):
        _observe(seen, numbers_in(row["check_ref"]), _parse_date(row["payment_date"]))
    if has_table(ctx, "expense_report"):
        for row in ctx.ledger.query(
            "SELECT instrument_ref, reimbursed_date, cleared_date FROM expense_report "
            "WHERE tenant = ? AND instrument_ref != ''",
            (slug,),
        ):
            when = _parse_date(row["cleared_date"] or row["reimbursed_date"] or "")
            _observe(seen, numbers_in(row["instrument_ref"]), when)
    if has_table(ctx, "events"):
        for row in ctx.ledger.query(
            "SELECT payload_json, created_at FROM events WHERE tenant = ? "
            "AND event_type = 'ap.reconcile.unknown'",
            (slug,),
        ):
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            when = _parse_date(payload.get("date") or row["created_at"])
            _observe(seen, numbers_in(str(payload.get("check_ref") or "")), when)
    return seen


def _runs(missing: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for n in missing:
        if runs and runs[-1][1] == n - 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.check_gaps_enabled or not has_table(ctx, "ap_invoices"):
        return []
    seen = _observed(ctx)
    watched = set(ctx.tenant.check_gaps_series)
    cutoff = ctx.now.date() - timedelta(days=ctx.tenant.check_gaps_window_days)

    by_series: dict[str, list[int]] = {}
    for n in sorted(seen):
        by_series.setdefault(_series(n), []).append(n)

    findings: list[Finding] = []
    for series, numbers in sorted(by_series.items()):
        if watched and series not in watched:
            continue
        if len(numbers) < ctx.tenant.check_gaps_min_observed:
            continue
        for lower, upper in zip(numbers, numbers[1:], strict=False):
            if upper - lower <= 1 or seen[upper] < cutoff:
                continue
            missing = list(range(lower + 1, upper))
            for lo, hi in _runs(missing):
                label = f"{lo}" if lo == hi else f"{lo}-{hi}"
                names = ", ".join(str(n) for n in range(lo, hi + 1))
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=f"{series} {label}",
                        condition="check-gap",
                        severity="WARN",
                        detail=f"check(s) {names} missing between {lower} "
                        f"({seen[lower].isoformat()}) and {upper} ({seen[upper].isoformat()}); "
                        "no settled ledger row, expense reimbursement, or bank clearing "
                        "names them: a hand-written check that never reached the ledger, "
                        "or a voided blank; record the payment or note the void",
                    )
                )
    return findings
