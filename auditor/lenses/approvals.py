"""Lens 6 — approval hygiene: the queue is a checkpoint, not a parking lot.

Four conditions, from the auditor's own SQL over ``approval_queue`` and the
engine's event record:

- a pending card older than the staleness window is a decision nobody made;
- a card stamped ``human_only`` (#356) resolved without the queue's record
  that a person at a terminal decided it is a decision an agent made;
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

import base64
import binascii
import hashlib
import json
import struct
import tomllib
from datetime import datetime
from pathlib import Path

from ..config import default_tenants_dir
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


# The doorkeeper's decision challenge (doorkeeper/witness.py), the auditor's
# own copy: SHA-256 over a domain tag, the nonce, the person who tapped, the
# card, the answer and the words they read, each length-prefixed.
WITNESS_DOMAIN = b"tim-witness-v1"
DOOR_VIA = {"door": "", "door-vouched": "vouched"}
VERBS = {"approved": "approve", "rejected": "reject"}


def _b64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def decision_challenge(nonce: bytes, person: str, card: str, verb: str, summary: str) -> bytes:
    h = hashlib.sha256(WITNESS_DOMAIN)
    for part in (nonce, person.encode(), card.encode(), verb.encode(), summary.encode()):
        h.update(struct.pack(">I", len(part)) + part)
    return h.digest()


def _authority(ctx: AuditContext) -> dict | None:
    root = Path(ctx.tenants_dir) if ctx.tenants_dir else default_tenants_dir()
    path = root / ctx.tenant.slug / "authority.toml"
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def door_refusal(ctx: AuditContext, row: dict, params: dict) -> str:
    """Why a decision the queue says came through the box's door (#465) is
    not one the auditor can stand behind, or "". The signature math is the
    doorkeeper's; the auditor checks the tenant opened the door for this
    kind of card, the person who tapped is the decider or the voucher the
    ministry named, and the signed answer is about this decision."""
    authority = _authority(ctx)
    if authority is None:
        return "the tenant has no authority.toml to open the door"
    door = (authority.get("presence") or {}).get("door") or []
    resource = str(params.get("witness_resource", ""))
    if not resource or resource not in door:
        return f"authority.toml's [presence] door does not open {resource or 'this card'}"
    people = authority.get("people") or {}
    by = str(params.get("decided_by", ""))
    if by not in people:
        return f"{by or 'nobody'} is not a person in authority.toml"
    tapper = by
    if DOOR_VIA.get(str(params.get("decided_via"))) == "vouched":
        tapper = str(params.get("vouched_by", ""))
        if (authority.get("vouch") or {}).get(by) != tapper or tapper not in people:
            return f"authority.toml does not name {tapper or 'anyone'} to vouch for {by}"
    try:
        evidence = json.loads(params.get("witness") or "")
        client = json.loads(_b64u(str(evidence["client_data"])))
        nonce = _b64u(str(evidence["nonce"]))
        signed = _b64u(str(client["challenge"]))
        summary = str(evidence["summary"])
        if not evidence.get("signature") or client.get("type") != "webauthn.get":
            raise ValueError("no signed answer")
    except (ValueError, KeyError, TypeError, binascii.Error):
        return "the signed evidence cannot be read"
    verb = VERBS.get(str(row["status"]), "")
    if signed != decision_challenge(nonce, tapper, str(row["id"]), verb, summary):
        return f"the signed evidence is not {tapper}'s {verb} on this card"
    return ""


def check_human_only(ctx: AuditContext) -> list[Finding]:
    """A card stamped human-only (#356) and resolved without the queue's
    record that a person decided it: at a terminal, or through the box's
    door with their own Face ID or the Face ID of the person the ministry
    named to vouch for them (#465; the owner counts both the same). The
    queue refuses any other route, so a hit means the decision came some
    other way: a faked terminal, a direct write to the queue, or a bug in
    the gate. CRITICAL: the stamp exists because no agent may make this
    decision."""
    findings: list[Finding] = []
    for row in ctx.ledger.query(
        "SELECT * FROM approval_queue WHERE tenant = ? "
        "AND status IN ('approved', 'rejected') ORDER BY id",
        (ctx.tenant.slug,),
    ):
        params = _params(row)
        if params.get("human_only") != "true" or params.get("decided_via") == "terminal":
            continue
        why = "no record that a person decided it at a terminal or by Face ID"
        if params.get("decided_via") in DOOR_VIA:
            refused = door_refusal(ctx, row, params)
            if not refused:
                continue
            why = f"a decision through the door the auditor cannot stand behind: {refused}"
        about = params.get("extracted_vendor") or params.get("file") or "?"
        findings.append(
            Finding(
                lens=LENS,
                subject=f"approval #{row['id']} {row['action_type']}",
                condition="human-only-decided-by-agent",
                severity="CRITICAL",
                detail=f"human-only card ({about}) was {row['status']} on "
                f"{str(row['resolved_at'])[:10]} with {why}; re-open the decision with "
                "the owner",
            )
        )
    return findings


def check(ctx: AuditContext) -> list[Finding]:
    return [
        *check_stale_pending(ctx),
        *check_human_only(ctx),
        *check_approved_batches_consumed(ctx),
        *check_swallowed_asks(ctx),
    ]
