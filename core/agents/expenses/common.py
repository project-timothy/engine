"""Helpers every expenses job shares: the tenant's month and clock, a person's
folders and name, the event-state readers, and placing a file by copy.
"""

from __future__ import annotations

import errno
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ...engine import clock
from ...engine.contracts import JobContext
from ...engine.fileops import CannotVerify, place_copy
from ...engine.result import Anomaly
from .records import (
    LANDED_EVENT,
    PRICED_EVENT,
    PROPOSED_EVENT,
    REFERENCE_EVENT,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tenant_month(ctx: JobContext) -> str:
    """The tenant's current wall-clock month (#136): a UTC month flips
    hours early on the tenant's evening of the 31st."""
    return clock.local_month(ctx.tenant.identity.timezone)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _person_slug(name: str) -> str:
    """ "Alex Doe" -> "Doe_Alex" (the legacy live-report convention)."""
    parts = [p for p in name.replace("_", " ").split() if p]
    if len(parts) >= 2:
        return f"{parts[-1]}_{' '.join(parts[:-1])}".replace(" ", "_")
    return name.replace(" ", "_")


def _drop_dir(ctx: JobContext) -> Path | None:
    configured = ctx.params.get("drop_dir") or ctx.tenant.expenses.drop_dir
    return Path(configured).expanduser() if configured else None


def _filing_dir(ctx: JobContext) -> Path:
    configured = ctx.params.get("filing_dir") or ctx.tenant.expenses.filing_dir
    if not configured:
        raise ValueError(
            "no expenses filing directory: set [expenses].filing_dir in tenant.toml "
            "or pass --param filing_dir=PATH"
        )
    return Path(configured).expanduser()


def _persons(ctx: JobContext) -> list:
    return list(ctx.tenant.expenses.persons)


def _person_named(ctx: JobContext, name: str):
    return next((p for p in _persons(ctx) if p.name == name), None)


# ---- event-state readers ----------------------------------------------------


def _events_of(ctx: JobContext, event_type: str) -> list[dict]:
    return [e for e in ctx.ledger.read_event_log() if e.get("event_type") == event_type]


def _landed_by_sha(ctx: JobContext) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in _events_of(ctx, LANDED_EVENT):
        payload = e.get("payload", {})
        sha = str(payload.get("sha256", ""))
        if sha:
            out[sha] = payload
    return out


def _proposals_by_sha(ctx: JobContext) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in _events_of(ctx, PROPOSED_EVENT):
        payload = e.get("payload", {})
        sha = str(payload.get("sha256", ""))
        if sha:
            out[sha] = payload
    for sha, price in _owner_prices_by_sha(ctx).items():
        out[sha] = _apply_owner_price(out.get(sha, {}), price)
    return out


def _owner_prices_by_sha(ctx: JobContext) -> dict[str, dict]:
    """Owner price directives already recorded (``expense.owner_priced``),
    latest per receipt. They overlay the extractor's proposal so a receipt
    the model could not price (or priced wrong) carries the owner's value
    on every later run, including the approval that builds the report."""
    out: dict[str, dict] = {}
    for e in _events_of(ctx, PRICED_EVENT):
        payload = e.get("payload", {})
        sha = str(payload.get("sha256", ""))
        if sha:
            out[sha] = payload
    return out


def _apply_owner_price(proposal: dict, price: dict) -> dict:
    """The proposal with the owner's amount/category laid over it. The
    extractor's "no amount found" flag is resolved by construction; the
    owner's provenance rides the flags so the review card shows it."""
    merged = dict(proposal)
    merged["amount_cents"] = int(price["amount_cents"])
    merged["category"] = str(price.get("category") or merged.get("category") or "")
    if price.get("note"):
        merged["note"] = str(price["note"])
    flags = [f for f in merged.get("flags", []) if not str(f).startswith("no amount found")]
    flags.append(
        f"amount owner-supplied (${int(price['amount_cents']) / 100:,.2f}"
        + (f": {price['note']}" if price.get("note") else "")
        + ")"
    )
    merged["flags"] = flags
    return merged


def _reference_shas(ctx: JobContext) -> set[str]:
    return {
        str(e.get("payload", {}).get("sha256", ""))
        for e in _events_of(ctx, REFERENCE_EVENT)
        if e.get("payload", {}).get("sha256")
    }


def _approved_cards(ctx: JobContext, action_type: str) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM approval_queue WHERE tenant = ? AND action_type = ? AND status = 'approved'",
        (ctx.tenant_slug, action_type),
    ).fetchall()
    out = []
    for r in rows:
        card = dict(r)
        card["params"] = json.loads(card.pop("params_json") or "{}")
        out.append(card)
    return out


def _sha256_or_none(path: Path) -> str | None:
    """The placement hash: None for a cloud-only placeholder (EDEADLK), so the
    copy contract can say "cannot verify" instead of guessing."""
    try:
        return _sha256(path)
    except OSError as exc:
        if exc.errno == errno.EDEADLK:
            return None
        raise


def _file_copy(src: Path, dest_dir: Path, *, guard, shadow: bool) -> Path:
    """Guarded, never-overwriting, read-back-verified copy (the
    core.engine.fileops contract, honesty audit 2026-09-03 03-F4). Raises
    CannotVerify for a placeholder on either side and CopyMismatch when the
    destination reads back different content; a "filed" claim downstream
    always stands on a verified copy."""
    return place_copy(
        src, dest_dir / src.name, guard=guard, shadow=shadow, hash_fn=_sha256_or_none
    ).dest


def _placement_anomaly(prefix: str, name: str, exc: OSError) -> Anomaly:
    if isinstance(exc, CannotVerify):
        return Anomaly(
            code=f"{prefix}.cloud_only_deferred",
            detail=f"{name}: cannot verify the filing {exc.side} "
            f"({Path(exc.filename).name} is a cloud-only placeholder); deferred",
        )
    return Anomaly(
        code=f"{prefix}.copy_unverified",
        detail=f"{name}: the copy read back different content; removed, retried on a later run",
    )


def _payload_sha(e: dict) -> str:
    return str(e.get("payload", {}).get("sha256", ""))
