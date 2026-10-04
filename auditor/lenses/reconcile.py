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

A clearing under the tenant's hand-check floor (``[qbo].direct_payment_
floor_cents``, default $600, issue #370) never WARNs here either. The
direct-payment lane (``core/agents/ap/direct_payment.py``) reads the exact
same tenant setting and refuses to card anything under it at any amount --
one named number, read by the lane and the auditor alike, never the lens's
own copy -- so a WARN below that line would have no answer path but a hand
mute. Each surfaced finding names the floor it cleared.

Four clearings are not unknown money however loudly the event log says so,
and all four are the same shape: the log is append-only, so a clearing the
book has since ANSWERED keeps its stale event as the newest word, and the
finding would re-ask about settled money for the rest of the window. Each is
re-derived from ledger state rather than read off an engine verdict, which
is the whole point of auditing independently.

1. The accounting-system Purchase the engine wrote for one of this ledger's
   own EXPENSE REPORTS. A report commits money with no ``ap_invoices`` row,
   so before the engine learned that join every report that ever cleared was
   logged unknown. ``qbo_purchase_id`` on ``expense_report`` is the exact id
   the engine recorded at ``expenses match``, so that report IS the ledger
   row this clearing matched.
2. A clearing the HAND-CHECK LANE has already turned into a payable row. That
   lane closes its own loop in three steps — log the unknown once, park an
   ``ap.record_direct_payment`` card, create the row on the owner's approval
   — and reading only step one meant a clearing the owner answered and the
   engine recorded stayed WARN until a hand mute or the window removed it.
   The lane writes the clearing's id verbatim into ``ap_invoices.source_file``
   under its own ``DP-`` number, so the join reads an id, not a filename
   convention this package would have to re-spell (core never imports
   auditor and auditor never imports core).
3. A clearing whose ``ap.record_direct_payment`` card the owner REJECTED
   (issue #372, owner decision 2026-09-28). Rejecting is an answer too —
   "this is not AP" — even though nothing lands in ``ap_invoices``, so the
   card itself, sitting in ``approval_queue`` with ``status = 'rejected'``,
   is the only ledger truth naming the decision. The finding still closes,
   but :func:`resolve_notes` names WHY on the resolved line instead of
   letting the stale WARN text stand as if nothing was ever said about it.
4. A clearing the owner answered on the SWEEP lane's card (2026-10-02). One
   physical check reaches him through more than one lane, and the engine acts
   on that: ``_carded_checks`` in ``core/agents/ap/jobs.py`` holds every
   ``(normalized check, cents)`` pair already in front of the owner from
   EITHER card source, and the hand-check lane refuses to park a second card
   for a check the sweep's note already carries -- one question, one answer.
   So a check that clears on the statement after the sweep named it never
   gets an ``ap.record_direct_payment`` card at all, and exits 2 and 3, both
   keyed on the clearing's ``qbo_id``, can never fire for it. A sweep card
   carries no ``qbo_id`` -- its identity is the bank feed's own
   ``feed_line_id`` -- so this join reads the instrument instead: the check
   number plus the amount to the cent, the same pair the engine itself calls
   one physical check. Both sides are the bank's facts, which is why this is
   identity and not resemblance; a clearing with no check reference joins
   nothing rather than degrading to amount-matching.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from ..findings import Finding
from . import AuditContext
from ._tables import dollars, has_table

LENS = "reconcile"
EVENT = "ap.reconcile.unknown"
# core/agents/ap/jobs.py's own constants, duplicated: auditor never imports core.
DIRECT_PAYMENT_CARD = "ap.record_direct_payment"
SWEEP_CARD = "qbo.sweep_parked"
_CHECK_PREFIX = re.compile(r"^(check|chk|ck|no|num)")
_CHECK_JUNK = re.compile(r"[^a-z0-9]")


def _norm_check_ref(ref: object) -> str:
    """``core.agents.ap.reconcile.norm_check_ref``'s contract, re-spelled.

    "Check 8158" and "8158" are one instrument, and both spellings reach this
    ledger: the statement parser writes the bank's bare number, while a sweep
    note is prose a session wrote. The engine's copy is the authority, and the
    two are pinned against each other by a parity eval rather than by hope.
    """
    token = _CHECK_JUNK.sub("", str(ref or "").lower())
    return _CHECK_PREFIX.sub("", token)


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


def _recorded_direct_payments(ctx: AuditContext) -> set[str]:
    """Clearing ids the hand-check lane has already recorded as a payable.

    Two clauses, both written by that lane's execution step and neither one
    enough alone. ``source_file`` carries the clearing's own id verbatim, so
    the match is identity rather than a heuristic; the ``DP-`` number fences
    it to rows this lane wrote, so an ordinary invoice can never close a
    finding by coincidence. The whole payment id is compared, entity
    included, so ``BillPayment:313`` never answers for ``Purchase:313``.
    """
    if not has_table(ctx, "ap_invoices"):
        return set()
    return {
        str(row["source_file"])
        for row in ctx.ledger.query(
            "SELECT source_file FROM ap_invoices WHERE tenant = ? "
            "AND source_file != '' AND invoice_number LIKE 'DP-%'",
            (ctx.tenant.slug,),
        )
    }


def _rejected_direct_payment_cards(ctx: AuditContext) -> dict[str, dict[str, object]]:
    """Clearing id -> the rejected hand-check card that answered it (its
    approval-queue id and the date the owner decided).

    Unlike the approved path, nothing lands in ``ap_invoices`` for a
    rejection — the approval queue itself, read as a fact the owner already
    recorded there rather than an engine verdict, is the only place this
    decision lives. ``qbo_id`` is the same field the lane writes into the
    card's own params at park time (``core/agents/ap/jobs.py``); the auditor
    reads it back rather than re-deriving it.
    """
    if not has_table(ctx, "approval_queue"):
        return {}
    out: dict[str, dict[str, object]] = {}
    for row in ctx.ledger.query(
        "SELECT id, params_json, resolved_at, created_at FROM approval_queue WHERE tenant = ? "
        "AND action_type = ? AND status = 'rejected'",
        (ctx.tenant.slug, DIRECT_PAYMENT_CARD),
    ):
        try:
            params = json.loads(row["params_json"])
        except json.JSONDecodeError:
            continue
        qbo_id = str(params.get("qbo_id") or "")
        if not qbo_id:
            continue
        when = _parse_date(row["resolved_at"] or row["created_at"])
        out[qbo_id] = {"id": row["id"], "date": when}
    return out


def _decided_sweep_cards(ctx: AuditContext) -> dict[tuple[str, int], dict[str, object]]:
    """(normalized check, cents) -> the DECIDED sweep card that answered it.

    The pair is the engine's own cross-source identity for one physical
    check (``_carded_checks``), which is exactly why the hand-check lane never
    parked a card for a clearing the sweep's note already named. A card with
    no check reference keys nothing: its feed line may be a card swipe, and
    amount alone would close findings by coincidence.

    Decided, never pending. A pending card is the question still being asked,
    and this lens stays loud about an unanswered clearing however many
    surfaces carry it -- the same rule exit 3 applies to a hand-check card.
    """
    if not has_table(ctx, "approval_queue"):
        return {}
    out: dict[tuple[str, int], dict[str, object]] = {}
    for row in ctx.ledger.query(
        "SELECT id, params_json, status, resolved_at, created_at FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status != 'pending'",
        (ctx.tenant.slug, SWEEP_CARD),
    ):
        try:
            params = json.loads(row["params_json"])
        except json.JSONDecodeError:
            continue
        check_ref = _norm_check_ref(params.get("check_ref"))
        cents = int(params.get("amount_cents") or 0)
        if not check_ref or cents <= 0:
            continue
        out[(check_ref, cents)] = {
            "id": row["id"],
            "status": str(row["status"]),
            "account": str(params.get("account") or params.get("match") or "").strip(),
            "check_ref": str(params.get("check_ref") or "").strip(),
            "date": _parse_date(row["resolved_at"] or row["created_at"]),
        }
    return out


def _sweep_key(payload: dict[str, object]) -> tuple[str, int]:
    """The instrument this clearing is, as a sweep card would key it."""
    return (_norm_check_ref(payload.get("check_ref")), int(payload.get("amount_cents") or 0))


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.reconcile_enabled or not has_table(ctx, "events"):
        return []
    cutoff = ctx.now.date() - timedelta(days=ctx.tenant.reconcile_unknown_window_days)
    floor_cents = ctx.tenant.qbo_direct_payment_floor_cents
    recorded_reports = _expense_report_purchases(ctx)
    recorded_payments = _recorded_direct_payments(ctx)
    rejected_payments = _rejected_direct_payment_cards(ctx)
    swept = _decided_sweep_cards(ctx)
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
        # The hand-check lane already recorded this clearing as a payable on
        # the owner's approval (7.4). The money is in the book; the unknown
        # event is just the oldest thing anyone wrote about it.
        if qbo_id in recorded_payments:
            continue
        # The owner already answered this one too — "not AP" — through the
        # approval queue rather than a ledger row (7.4's third exit).
        if qbo_id in rejected_payments:
            continue
        # The sweep lane put this very check in front of him instead, and he
        # answered it there (2026-10-02). The hand-check lane never carded it
        # for exactly that reason, so no qbo_id-keyed exit above can see the
        # answer; the instrument can.
        if _sweep_key(payload) in swept:
            continue
        # Below the tenant's hand-check floor (issue #370, owner decision
        # 2026-09-28), the direct-payment lane will never card this clearing
        # at any tenant setting -- so a WARN on it has no answer path but a
        # hand mute. The smallest honest change: stay as quiet about it as
        # the lane already is, rather than inventing a severity for an item
        # nothing can ever close.
        if int(payload.get("amount_cents") or 0) < floor_cents:
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
                f"cleared the bank above the {dollars(floor_cents)} hand-check floor and "
                "matched no ledger row (the engine's reconcile logged it as unknown); a "
                "hand-written check or an unlogged payment: record the row, or leave it "
                "if it is not AP (a pay-out, payroll, a card paydown)",
            )
        )
    return findings


def resolve_notes(ctx: AuditContext) -> dict[str, str]:
    """Fingerprint -> resolve reason for a clearing this lens is about to
    stop warning on because the owner REJECTED its hand-check card.

    The runner passes this to :meth:`AuditorStore.reconcile`'s
    ``resolve_reasons`` (issue #372): a closing finding otherwise carries its
    last WARN detail onto the resolved line unchanged, which would read as
    if the clearing simply stopped mattering. It did not — the owner decided
    it — and this is the one fact the lens knows that the generic reconcile
    pass does not: which fingerprint is closing, and why. Every entry here is
    harmless to hand to the store even for a fingerprint that was never open:
    the store only ever touches a fingerprint it is already resolving.
    """
    if not ctx.tenant.reconcile_enabled or not has_table(ctx, "events"):
        return {}
    recorded_reports = _expense_report_purchases(ctx)
    recorded_payments = _recorded_direct_payments(ctx)
    notes: dict[str, str] = {}

    def when_text(card: dict[str, object]) -> str:
        when = card["date"]
        return when.isoformat() if when else "an unknown date"

    def fingerprint(qbo_id: str) -> str:
        return Finding(
            lens=LENS, subject=qbo_id, condition="unknown-clearing", severity="WARN", detail=""
        ).fingerprint

    for qbo_id, card in _rejected_direct_payment_cards(ctx).items():
        if _is_expense_report_purchase(qbo_id, recorded_reports) or qbo_id in recorded_payments:
            continue  # the money turned out to be recorded after all
        notes[fingerprint(qbo_id)] = f"owner rejected card #{card['id']} on {when_text(card)}"

    # The sweep lane's answer (exit 4). Only the event knows which clearing id
    # an instrument belongs to, so the walk is over the events, not the cards.
    swept = _decided_sweep_cards(ctx)
    if not swept:
        return notes
    for row in ctx.ledger.query(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant.slug, EVENT),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        qbo_id = str(payload.get("qbo_id") or "")
        card = swept.get(_sweep_key(payload)) if qbo_id else None
        if card is None:
            continue
        if _is_expense_report_purchase(qbo_id, recorded_reports) or qbo_id in recorded_payments:
            continue  # the money turned out to be recorded after all
        ref = card["check_ref"] or payload.get("check_ref") or ""
        if card["status"] == "rejected":
            notes[fingerprint(qbo_id)] = (
                f"owner rejected sweep card #{card['id']} on {when_text(card)}"
            )
        else:
            notes[fingerprint(qbo_id)] = (
                f"owner coded check {ref} to {card['account'] or 'an account'} "
                f"on sweep card #{card['id']}, {when_text(card)}"
            )
    return notes
