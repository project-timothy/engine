"""Lens 6 — approval hygiene: the queue is a checkpoint, not a parking lot.

Three conditions, from the auditor's own SQL over ``approval_queue`` and the
engine's event record:

- a pending card older than the staleness window is a decision nobody made;
- an APPROVED bills batch must be fully consumed — every covered row either
  carries a bill id, sits behind a parked mapping/duplicate card, or has
  SETTLED since approval (settlement closes the AP question by another route:
  a bill the owner paid on a card with an explicit no-bill note — whether the
  accounting system agrees is lens 7's question, not this one's). An approval
  whose follow-through never happened is how "I approved that" and "it never
  posted" coexist quietly;
- an ask the engine raised that the queue DROPPED against a card the owner
  already answered. The queue dedups on a stable subject key: a collision
  with a pending card is the designed memory (the card still holds the ask),
  but a collision with a resolved one swallows the question, and the engine
  records that as an event and a run anomaly rather than staying silent.
  Nothing read that record, so the drop was visible in the ledger and absent
  from the report — which is the surface the owner actually reads. This check
  is its delivery surface, and the queue's own emptiness is why it is needed:
  the lane asked, no card exists, and nothing else can tell.
"""

from __future__ import annotations

import json
from datetime import datetime

from ..findings import Finding
from . import AuditContext

LENS = "approvals"

PUSH_BATCH = "ap.qbo_push_batch"
# The engine's name for an ask the queue absorbed into an already-resolved
# card (core/engine/runner.py; pinned by tests/unit/test_runner_approval_swallow.py).
SWALLOWED = "engine.approval_swallowed"
# A drop is instantaneous: it cannot resolve itself the way a pending card
# can be answered, so the report would carry it forever. Aging it out is what
# lets the checklist reconciliation close the item once the lane stops
# re-asking — the same rule the heartbeat lens applies to preflight markers.
# A lane that re-asks keeps the item alive on its own.
SWALLOWED_LOOKBACK_DAYS = 14
# Cards the push job parks per row instead of writing; a covered row behind
# one of these is accounted for, not forgotten.
PARKED_TYPES = ("ap.qbo_map_vendor", "ap.qbo_map_account", "ap.qbo_duplicate_review")
# The auditor's own copy of the settled statuses (see status_coherence).
SETTLED = frozenset({"Paid", "Void - Already Paid", "Void - Duplicate", "Cancelled"})


def _age_days(created_at: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(created_at)).total_seconds() / 86400


def _params(row: dict) -> dict:
    try:
        return json.loads(row["params_json"])
    except json.JSONDecodeError:
        return {}


def _payload(row: dict) -> dict:
    """An event payload, or an empty one. A payload the auditor cannot parse
    must never downgrade a drop back into silence: the row's existence is the
    finding, its contents are the detail."""
    try:
        parsed = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def check_stale_pending(ctx: AuditContext) -> list[Finding]:
    findings: list[Finding] = []
    for row in ctx.ledger.query(
        "SELECT * FROM approval_queue WHERE tenant = ? AND status = 'pending' ORDER BY id",
        (ctx.tenant.slug,),
    ):
        age = _age_days(row["created_at"], ctx.now)
        if age > ctx.tenant.stale_approval_days:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=f"approval #{row['id']} {row['action_type']}",
                    condition="stale-pending",
                    severity="WARN",
                    detail=f"waiting for a decision for {age:.0f} day(s) "
                    f"(window {ctx.tenant.stale_approval_days}d)",
                )
            )
    return findings


def _parked_row_ids(ctx: AuditContext) -> set[str]:
    parked: set[str] = set()
    placeholders = ",".join("?" for _ in PARKED_TYPES)
    for row in ctx.ledger.query(
        f"SELECT params_json FROM approval_queue WHERE tenant = ? AND status = 'pending' "
        f"AND action_type IN ({placeholders})",
        (ctx.tenant.slug, *PARKED_TYPES),
    ):
        row_id = _params(row).get("row_id")
        if row_id is not None:
            parked.add(str(row_id))
    return parked


def check_approved_batches_consumed(ctx: AuditContext) -> list[Finding]:
    findings: list[Finding] = []
    parked = _parked_row_ids(ctx)
    for card in ctx.ledger.query(
        "SELECT * FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status = 'approved' ORDER BY id",
        (ctx.tenant.slug, PUSH_BATCH),
    ):
        row_ids = [rid for rid in _params(card).get("row_ids", "").split(",") if rid]
        unconsumed: list[str] = []
        for rid in row_ids:
            rows = ctx.ledger.query(
                "SELECT id, qbo_bill_id, status, vendor, invoice_number "
                "FROM ap_invoices WHERE id = ?",
                (int(rid),),
            )
            if not rows:
                unconsumed.append(f"row {rid} (no such invoice)")
                continue
            row = rows[0]
            settled = str(row["status"]) in SETTLED
            if not row["qbo_bill_id"] and not settled and rid not in parked:
                unconsumed.append(f"#{row['id']} {row['vendor']} / {row['invoice_number']}")
        if unconsumed:
            listed = "; ".join(unconsumed[:5])
            more = f" (+{len(unconsumed) - 5} more)" if len(unconsumed) > 5 else ""
            findings.append(
                Finding(
                    lens=LENS,
                    subject=f"approval #{card['id']} {PUSH_BATCH}",
                    condition="approved-not-executed",
                    severity="WARN",
                    detail=f"batch was approved but {len(unconsumed)} of {len(row_ids)} "
                    f"row(s) have no bill id and no parked card: {listed}{more}",
                )
            )
    return findings


def check_swallowed_asks(ctx: AuditContext) -> list[Finding]:
    """Asks the engine raised and the queue dropped, one finding per ask.

    Grouped by (action type, dedup key) because a lane re-asks every time its
    input re-keys and every re-ask hits the same resolved card: that is one
    unanswered question asked N times, not N questions. Severity is WARN, the
    same as a card nobody answered — this is a card nobody was OFFERED, and
    what it costs depends entirely on what the lane wanted, which only the
    owner can say.
    """
    drops: dict[tuple[str, str], list[dict]] = {}
    for row in ctx.ledger.query(
        "SELECT payload_json, created_at FROM events "
        "WHERE tenant = ? AND event_type = ? ORDER BY created_at",
        (ctx.tenant.slug, SWALLOWED),
    ):
        payload = _payload(row)
        ask = (str(payload.get("action_type") or "?"), str(payload.get("key") or "?"))
        drops.setdefault(ask, []).append({**payload, "at": row["created_at"]})

    findings: list[Finding] = []
    for (action_type, key), seen in sorted(drops.items()):
        latest = seen[-1]
        age = _age_days(latest["at"], ctx.now)
        if age > SWALLOWED_LOOKBACK_DAYS:
            continue
        blocker = latest.get("existing_id")
        status = str(latest.get("existing_status") or "already-resolved")
        when = (
            f"{len(seen)} times since {seen[0]['at'][:10]}, latest {latest['at'][:10]}"
            if len(seen) > 1
            else f"on {latest['at'][:10]}"
        )
        against = f"{status} card #{blocker}" if blocker is not None else f"a {status} card"
        findings.append(
            Finding(
                lens=LENS,
                subject=f"dropped ask {action_type} {key}",
                condition="swallowed",
                severity="WARN",
                detail=f"the engine raised this ask {when} ({age:.0f}d ago) and the queue "
                f"dropped it against {against}: no card carries the question, so nobody "
                "was asked and nothing is waiting on a decision",
            )
        )
    return findings


def check(ctx: AuditContext) -> list[Finding]:
    return [
        *check_stale_pending(ctx),
        *check_approved_batches_consumed(ctx),
        *check_swallowed_asks(ctx),
    ]
