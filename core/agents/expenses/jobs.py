"""Expenses agent jobs: intake, extract, report, match.

Design of record: ``docs/expenses-design.md`` (approved 2026-08-04). The spine
is deterministic; the LLM's only role is per-receipt field proposals that
land on a review card (invariant 2). Every QBO write is approval-gated,
provenance-stamped, duplicate-guarded, and readback-verified (the W1
contract). The engine never moves money (invariant 7): the owner reimburses,
The expenses agent records.
"""

from __future__ import annotations

import errno
import hashlib
import json
import re
import shutil
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from ...authority import CardRule
from ...engine import clock
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobHandler, JobOutput
from ...engine.fileops import CannotVerify, CopyMismatch, place_copy
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ...institution import HELD, SENT, claim_sink, submit
from ...policy import Spent, check_month
from ...tools.viewer import unit_of
from ..ap.extraction import ExtractionError, build_extractor, resolved_tier
from ..ap.schema import amount_to_cents
from ..policy.jobs import rules_in_force
from . import categories as cats
from . import fx, records, statements
from . import inbox as inbox_boundary
from .dedup import duplicate_groups
from .records import (
    AMOUNT_EVENT,
    CATEGORY_EVENT,
    DATED_EVENT,
    DRIVE_EVENT,
    LANDED_EVENT,
    LINE_EVENT,
    LINE_PURPOSE_EVENT,
    PRICED_EVENT,
    PROPOSED_EVENT,
    PURPOSE_EVENT,
    READ_EVENT,
    REFERENCE_EVENT,
    REVIEW_CARD,
    SCHOOL_EVENT,
    SEND_CARD,
    SENT_EVENT,
    STATUS_OPEN,
    STATUS_RECORDED,
    STATUS_REIMBURSED,
    TRANSFER_EVENT,
    drive_id,
)
from .scan_split import (
    GroupingError,
    build_grouper,
    pdf_page_count,
    split_pdf,
    validate_groups,
)
from .scan_split import resolved_tier as grouper_tier
from .schema import (
    RECEIPT_SUFFIXES,
    ManifestLine,
    ReportManifest,
    normalize_category,
    parse_amount_tag,
    parse_foreign_claim,
    parse_project_tag,
)

SPLIT_EVENT = "expense.scan_split"
# Issue #112: the Taildrop receipt-inbox pre-stage.
INBOX_CLASSIFIED_EVENT = "expense.inbox_classified"
INBOX_FILED_EVENT = "expense.inbox_filed"
INBOX_SKIPPED_EVENT = "expense.inbox_skipped"
INBOX_FILE_CARD = "expenses.inbox_file_receipt"
INBOX_SKIP_CARD = "expenses.inbox_skip"
SINGLE_EVENT = "expense.scan_single"
# Job-time durable records (honesty audit #170, 03-F9): written BEFORE a
# file moves, linked to the run when it lands, healed into the event above
# on the next run if the run died in between.
SPLIT_RECORD = "expense.scan_split.recorded"
INBOX_FILED_RECORD = "expense.inbox_filed.recorded"
INBOX_SKIPPED_RECORD = "expense.inbox_skipped.recorded"
UNKNOWN_FOLDER_EVENT = "expense.unknown_person_folder"

DRAFT_CARD = "expenses.report_draft"
CONFIRM_CARD = "expenses.reimbursement_record"
HELD_EVENT = "institution.claim.held"
BUILT_EVENT = "expense.report_built"


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


def _channel_for(ctx: JobContext, person_name: str) -> str:
    """The payment channel named on this person's confirm cards: the
    person-level override when set, else the tenant default (issue #120)."""
    person = _person_named(ctx, person_name)
    override = getattr(person, "default_channel", "") if person else ""
    return override or ctx.tenant.expenses.default_channel


def _payee_name(ctx: JobContext, person_name: str) -> str:
    """The accounting-system payee a report books to: the vendor record
    behind a vendor-role person, else the person's own name."""
    person = _person_named(ctx, person_name)
    return (getattr(person, "qbo_vendor", "") if person else "") or person_name


def _project_accounts(
    template: str, projects: list[str], account_map: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Resolve projects to chart account ids via the tenant template
    (vendor-role booking, issue #120): ``{number}`` is the project code
    minus its letter prefix (P26_2001 -> 26_2001). Anything unresolvable
    lands in ``missing`` — a card, never a guess, never a fallback to the
    per-category table."""
    ids: dict[str, str] = {}
    missing: list[str] = []
    for project in projects:
        number = re.sub(r"^[A-Za-z]+", "", str(project or ""))
        fqn = template.replace("{number}", number) if template and number else ""
        acct = account_map.get(fqn) if fqn else None
        if acct is None:
            missing.append(project or "(no project)")
        else:
            ids[project] = acct
    return ids, missing


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


def _consumed_shas(ctx: JobContext) -> set[str]:
    rows = ctx.ledger.conn.execute(
        "SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (ctx.tenant_slug,)
    ).fetchall()
    return {str(r[0]) for r in rows if r[0]}


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


def _pending_skip_cards(ctx: JobContext) -> list[dict]:
    """This lane's own pending skip cards, each with the ApprovalSpec key it was
    parked under (the subject key's tail), the shape skip_card_supersessions reads."""
    prefix = f"apr:{ctx.tenant_slug}:expenses:{INBOX_SKIP_CARD}:"
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM approval_queue WHERE tenant = ? AND action_type = ? AND status = 'pending'",
        (ctx.tenant_slug, INBOX_SKIP_CARD),
    ).fetchall()
    out = []
    for r in rows:
        subject = str(r["idempotency_key"])
        if not subject.startswith(prefix):
            continue
        out.append(
            {
                "id": r["id"],
                "action_type": r["action_type"],
                "status": r["status"],
                "key": subject[len(prefix) :],
                "params": json.loads(r["params_json"] or "{}"),
            }
        )
    return out


def skip_card_supersessions(cards: list[dict], shas) -> list[dict]:
    """Which pending skip cards tonight's skip card replaces (proposal 99e8ae57).

    Containment, never recency: a card is superseded only when every file it
    asks about is in tonight's set AND tonight's set is strictly larger (an
    equal set is the same key, which the queue's dedup already handles). A
    card holding one file tonight lacks, a card of another action type, and an
    answered card are all left alone. Oldest first."""
    tonight = set(shas)
    plan = []
    for card in sorted(cards, key=lambda c: c["id"]):
        if card.get("action_type") != INBOX_SKIP_CARD or card.get("status") != "pending":
            continue
        theirs = {s for s in str(card.get("params", {}).get("sha256s", "")).split(",") if s}
        if theirs and theirs < tonight:
            plan.append({"card_id": card["id"], "key": card["key"]})
    return plan


# ---- job: intake ------------------------------------------------------------


def _drop_candidates(ctx: JobContext) -> list[tuple[str, str, Path]]:
    """(person, project-or-'', file) for every receipt-suffixed file in the
    per-person drop tree. The project is the subfolder under the person dir
    (decision 2: folder first); '' means only a filename tag could answer."""
    drop = _drop_dir(ctx)
    if drop is None or not drop.is_dir():
        return []
    out: list[tuple[str, str, Path]] = []
    for person in _persons(ctx):
        person_dir = drop / person.name
        if not person_dir.is_dir():
            continue
        for path in sorted(person_dir.rglob("*")):
            if not (path.is_file() and path.suffix.lower() in RECEIPT_SUFFIXES):
                continue
            rel = path.relative_to(person_dir)
            project = rel.parts[0] if len(rel.parts) > 1 else ""
            out.append((person.name, project, path))
    return out


def _unknown_person_dirs(ctx: JobContext) -> list[tuple[str, list[Path]]]:
    """Top-level drop-tree folders matching no configured person, with the
    receipt-suffixed files inside each. Underscore-prefixed names
    (``_originals``) are reserved engine namespace. Visibility only (issue
    #119): the persons list stays the authority on treatment, so nothing
    here is ever filed — the live 2026-08-13 shape was a vendor's folder
    sitting invisible with everyone assuming its contents were processed."""
    drop = _drop_dir(ctx)
    if drop is None or not drop.is_dir():
        return []
    known = {p.name for p in _persons(ctx)}
    out: list[tuple[str, list[Path]]] = []
    for entry in sorted(drop.iterdir()):
        if not entry.is_dir() or entry.name in known or entry.name.startswith("_"):
            continue
        files = sorted(
            p for p in entry.rglob("*") if p.is_file() and p.suffix.lower() in RECEIPT_SUFFIXES
        )
        out.append((entry.name, files))
    return out


def _intake_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expintake")
    key.add("month", str(key.param("month") or _tenant_month(ctx)))  # the RESOLVED month (#135)
    key.param("grouper")
    key.param("filing_dir")
    key.config("expenses")
    key.config("books.cost_object")  # the project tag in file and folder names (#340)
    # Row 7.11: the split pass groups through the gateway, so the resolved
    # scan_group tier rides the key (docs/run-keys.md).
    key.config("llm")
    key.value("llm_tier", grouper_tier(ctx, _grouper_kind(ctx)))
    drop = [
        f"{who}/{proj}/{path.name}:{_sha256(path)}" for who, proj, path in _drop_candidates(ctx)
    ]
    key.value("drop", drop)
    # An unknown folder appearing with nothing else changing must break the
    # replay, or the runner returns the prior clean result and the warn
    # below never fires (the live 2026-08-13 shape).
    unknown: list[str] = []
    for folder, files in _unknown_person_dirs(ctx):
        unknown.append(f"unknown:{folder}")
        unknown.extend(f"unknown:{folder}/{f.name}:{_sha256(f)}" for f in files)
    key.value("unknown", unknown)
    # 03-F9: the archived originals are a run input too — an orphan (moved,
    # never recorded) appearing with nothing else changing must re-fire.
    if drop_dir := _drop_dir(ctx):
        key.value("originals", [f"{p.name}:{_sha256(p)}" for p in _archived_originals(drop_dir)])
    return key.digest()


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


def _grouper_kind(ctx: JobContext) -> str:
    import os

    return str(ctx.params.get("grouper") or os.environ.get("ENGINE_GROUPER") or "claude")


def _grouper(ctx: JobContext):
    return build_grouper(_grouper_kind(ctx), ctx)


def _scan_verdicts(ctx: JobContext) -> dict[str, str]:
    """Content hash -> verdict ("split" | "single") for every scan the
    splitter has judged, so a verdict is computed once, never per morning
    run. A "split" verdict is also the memory that says the original must
    never be filed whole (honesty audit 2026-09-03, 03-F3)."""
    out: dict[str, str] = {}
    for event_type, verdict in ((SPLIT_EVENT, "split"), (SINGLE_EVENT, "single")):
        for e in _events_of(ctx, event_type):
            sha = str(e.get("payload", {}).get("sha256", ""))
            if sha:
                out[sha] = verdict
    # A split recorded at job time whose run died before the event landed
    # (03-F9) is still a split: the original is archived, never re-split,
    # and a re-dropped copy is held.
    for rec in ctx.records(SPLIT_RECORD):
        sha = str(rec["payload"].get("sha256", ""))
        if sha:
            out.setdefault(sha, "split")
    return out


def _originals_dir(drop: Path) -> Path:
    return drop / "_originals"


def _archived_originals(drop: Path) -> list[Path]:
    """Top-level PDFs in the drop tree's ``_originals/`` (subfolders are a
    human's reconciled space and stay out of the walk)."""
    originals = _originals_dir(drop)
    if not originals.is_dir():
        return []
    return sorted(p for p in originals.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


def _orphaned_originals(ctx: JobContext, drop: Path) -> dict[str, Path]:
    """Archived originals with no ``expense.scan_split`` lineage event: the
    move landed and the run died before the record (honesty audit
    2026-09-03, 03-F9). The split pass cannot record before it moves (the
    runner persists events after the job returns), so the next run names
    every such orphan instead of treating the gone original as handled."""
    split_shas = {sha for sha, verdict in _scan_verdicts(ctx).items() if verdict == "split"}
    return {
        sha: path for path in _archived_originals(drop) if (sha := _sha256(path)) not in split_shas
    }


def _split_pass(
    ctx: JobContext,
) -> tuple[list[EventSpec], list[str], list[Anomaly], set[Path]]:
    """Combined-scan pre-pass (issue #104), upstream of filing.

    Detects multi-page receipt PDFs in the drop tree, asks the grouper for
    page groups, and splits by CODE into the same drop location, so the
    children flow through normal intake in the same run. The original moves
    to the drop tree's ``_originals/`` with a lineage event. A scan whose
    grouping fails or does not cover every page exactly once is HELD — never
    filed — because filing a 17-receipt scan as one line is the exact
    failure this pass exists to prevent.
    """
    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    held: set[Path] = set()

    drop = _drop_dir(ctx)
    pdfs = [
        (person, path)
        for person, _project, path in _drop_candidates(ctx)
        if path.suffix.lower() == ".pdf"
    ]
    if drop is None:
        return events, actions, anomalies, held

    # 03-F9, the healed half: a split recorded at job time whose event never
    # landed gets its event now, from the record.
    healed_events, healed_actions = _heal_split_records(ctx)
    events.extend(healed_events)
    actions.extend(healed_actions)

    # 03-F9: an archived original with no lineage event AND no job record
    # (a move from before records existed, or a hand copy) is never silence.
    # It flags every run it sits at the top of _originals/; a human
    # reconciles the children by hand and moves it into a subfolder.
    orphans = _orphaned_originals(ctx, drop)
    for path in orphans.values():
        anomalies.append(
            Anomaly(
                code="expenses.split_lineage_missing",
                detail=f"_originals/{path.name}: archived with no expense.scan_split "
                "lineage event (a run failed between the move and the record); its "
                "children, if any, intake as plain receipts with no page map. Reconcile "
                "by hand, then move the file into a subfolder of _originals/ to clear this",
            )
        )
    if not pdfs:
        return events, actions, anomalies, held

    verdicts: dict[str, str] | None = None
    landed: dict[str, dict] | None = None
    grouper = None
    for person, path in pdfs:
        pages = pdf_page_count(path)
        if pages <= 1:
            continue
        sha = _sha256(path)
        if verdicts is None:
            verdicts = _scan_verdicts(ctx)
            landed = _landed_by_sha(ctx)
        if sha in landed or verdicts.get(sha) == "single":
            continue
        if verdicts.get(sha) == "split":
            # 03-F3: the archived original came back (a sync client, a
            # human restore). The verdict is the memory: this scan was
            # split, its children were filed, and the whole scan must never
            # land as one receipt. Held, every run it sits there.
            held.add(path)
            anomalies.append(
                Anomaly(
                    code="expenses.split_original_redropped",
                    detail=f"{path.name}: this combined scan was already split and its "
                    "original archived to _originals/; a re-dropped copy is never filed "
                    "whole. Held in the drop tree; remove it by hand",
                )
            )
            continue
        if sha in orphans:
            # The half-moved shape: the copy into _originals/ landed and the
            # unlink did not. Splitting again would double the children.
            held.add(path)
            anomalies.append(
                Anomaly(
                    code="expenses.split_lineage_missing",
                    detail=f"{path.name}: a copy already sits in _originals/ with no "
                    "lineage event; held in the drop tree, never split twice",
                )
            )
            continue
        if grouper is None:
            grouper = _grouper(ctx)
        try:
            groups = grouper.propose_groups(path, pages)
        except GroupingError as exc:
            held.add(path)
            anomalies.append(
                Anomaly(
                    code="expenses.scan_split_failed",
                    detail=f"{path.name}: {exc}; held in the drop tree, nothing filed",
                )
            )
            continue
        reason = validate_groups(groups, pages)
        if reason:
            held.add(path)
            anomalies.append(
                Anomaly(
                    code="expenses.scan_split_invalid",
                    detail=f"{path.name}: {reason}; held in the drop tree, nothing filed",
                )
            )
            continue
        if len(groups) == 1:
            # The hotel-folio shape: one multi-page receipt, left whole. The
            # verdict event stops re-analysis on every future run.
            actions.append(f"multi-page single receipt: {path.name} left whole ({pages} pages)")
            events.append(
                EventSpec(
                    key=f"expscan-single:{sha}",
                    event_type=SINGLE_EVENT,
                    payload={"sha256": sha, "file": path.name, "person": person, "pages": pages},
                )
            )
            continue
        if ctx.shadow:
            held.add(path)
            actions.append(f"would split {path.name} into {len(groups)} receipts")
            continue

        originals_dir = drop / "_originals"
        archived = originals_dir / path.name
        n = 2
        while archived.exists():
            archived = originals_dir / f"{path.stem} ({n}){path.suffix}"
            n += 1
        ctx.guard.check_write(archived)
        ctx.guard.check_write(path.parent / "split-probe.pdf")
        children = split_pdf(path, groups, ctx.tenant.books.cost_object)
        child_payloads = [
            {"file": ch.path.name, "sha256": _sha256(ch.path), "pages": ch.pages} for ch in children
        ]
        originals_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sha256": sha,
            "file": path.name,
            "person": person,
            "pages": pages,
            "archived_to": str(archived),
            "children": child_payloads,
        }
        # Record-then-move (03-F9): the lineage is durable BEFORE the
        # original leaves the drop tree, so a death after the move leaves
        # a record the next run turns into the event (never an orphan).
        ctx.record_now(f"expscan-split:{sha}", SPLIT_RECORD, payload)
        shutil.move(str(path), archived)
        actions.append(
            f"split {path.name} ({pages} pages) into {len(children)} receipts; "
            "original -> _originals/"
        )
        events.append(
            EventSpec(key=f"expscan-split:{sha}", event_type=SPLIT_EVENT, payload=payload)
        )
    return events, actions, anomalies, held


def _heal_split_records(ctx: JobContext) -> tuple[list[EventSpec], list[str]]:
    """Job records whose lineage event never landed (the run died after the
    move, 03-F9): emit the event now from the record, so the page map and
    child hashes reach the event log exactly once."""
    recorded_shas = {
        str(e.get("payload", {}).get("sha256", "")) for e in _events_of(ctx, SPLIT_EVENT)
    }
    events: list[EventSpec] = []
    actions: list[str] = []
    for rec in ctx.records(SPLIT_RECORD):
        payload = rec["payload"]
        sha = str(payload.get("sha256", ""))
        if not sha or sha in recorded_shas:
            continue
        recorded_shas.add(sha)
        events.append(
            EventSpec(key=f"expscan-split:{sha}", event_type=SPLIT_EVENT, payload=payload)
        )
        actions.append(
            f"recorded split lineage for {payload.get('file', sha[:12])} from its job record "
            "(a run died between the move and the record)"
        )
    return events, actions


def _intake_run(ctx: JobContext) -> JobOutput:
    drop = _drop_dir(ctx)
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []

    if drop is None:
        return JobOutput(status="ok", summary="expenses intake: no drop_dir configured")
    if not drop.is_dir():
        return JobOutput(
            status="ok",
            summary=f"expenses intake: drop dir {drop} does not exist yet",
            anomalies=[
                Anomaly(
                    code="expenses.drop_dir_missing",
                    detail=f"create {drop} with one subfolder per person to start the flow",
                )
            ],
        )

    # Combined-scan pre-pass (issue #104): splits land next to the original,
    # so the fresh _drop_candidates() walk below intakes them this same run.
    split_events, split_actions, split_anomalies, held = _split_pass(ctx)
    events.extend(split_events)
    actions.extend(split_actions)
    anomalies.extend(split_anomalies)

    month = str(ctx.params.get("month") or _tenant_month(ctx))
    filing = _filing_dir(ctx)
    landed = _landed_by_sha(ctx)

    filed = already = dup = carded = 0
    for person, folder_project, path in _drop_candidates(ctx):
        if path in held:
            continue
        sha = _sha256(path)
        prior = landed.get(sha)
        if prior is not None:
            if str(prior.get("file", "")) == path.name:
                # The normal case: a filed receipt left in the drop tree.
                # Counted apart from a duplicate (03-F15) — the owner reads
                # "duplicate" as a re-drop, and this is not one.
                already += 1
                continue
            # Same bytes under a new name: the 2026-05-18 shape. Noop
            # with a card note, exactly as designed — never a second row.
            approvals.append(
                ApprovalSpec(
                    key=f"expdup:{sha[:16]}:{path.name}",
                    action_type="expenses.duplicate_drop",
                    params={
                        "person": person,
                        "file": path.name,
                        "original": str(prior.get("file", "")),
                    },
                    reason="same receipt re-dropped under a new name; the original "
                    "is already filed — approve to acknowledge, nothing was duplicated",
                )
            )
            dup += 1
            continue
        tag = parse_project_tag(path.name, ctx.tenant.books.cost_object)
        project = folder_project or tag
        if not project:
            carded += 1
            approvals.append(
                ApprovalSpec(
                    key=f"expattr:{sha[:16]}",
                    action_type="expenses.attribute_receipt",
                    params={"person": person, "file": path.name, "path": str(path)},
                    reason="no project subfolder and no filename tag; move the file "
                    "into a project subfolder — attribution is never guessed",
                )
            )
            continue
        if (
            folder_project
            and tag
            and parse_project_tag(folder_project, ctx.tenant.books.cost_object) not in ("", tag)
        ):
            anomalies.append(
                Anomaly(
                    code="expenses.attribution_disagrees",
                    detail=f"{path.name}: filename tag {tag} vs folder {folder_project}; "
                    "the folder wins (decision 2), flag kept for the review card",
                )
            )
        dest_dir = filing / month / "receipts" / person / project
        try:
            dest = _file_copy(path, dest_dir, guard=ctx.guard, shadow=ctx.shadow)
        except (CannotVerify, CopyMismatch) as exc:
            anomalies.append(_placement_anomaly("expenses", path.name, exc))
            continue
        if ctx.shadow:
            actions.append(f"would file {path.name} -> {dest_dir}")
            continue
        filed += 1
        landed[sha] = {"file": dest.name, "person": person}
        actions.append(f"filed {dest.name} for {person} ({project})")
        events.append(
            EventSpec(
                key=f"expense-receipt:{sha}",
                event_type=LANDED_EVENT,
                payload={
                    "sha256": sha,
                    "file": dest.name,
                    "person": person,
                    "project": project,
                    "month": month,
                    "filed_to": str(dest),
                    "drop_path": str(path),
                },
            )
        )

    # Unknown-person visibility (issue #119): a top-level folder for anyone
    # not in [expenses].persons — a vendor, a typo'd name, a future hire —
    # was silently invisible. Warn once per folder (the event is the memory);
    # receipt files inside escalate to a card keyed on the file set, so new
    # files raise a fresh card. Nothing here is ever processed.
    warned = {
        str(e.get("payload", {}).get("folder", "")) for e in _events_of(ctx, UNKNOWN_FOLDER_EVENT)
    }
    unknown = _unknown_person_dirs(ctx)
    for folder, files in unknown:
        if folder not in warned:
            anomalies.append(
                Anomaly(
                    code="expenses.unknown_person_folder",
                    detail=f"drop tree folder '{folder}' matches no configured expense "
                    f"person ({len(files)} receipt file(s) inside); nothing there is "
                    "processed — add the person to [expenses].persons or move the files",
                )
            )
            events.append(
                EventSpec(
                    key=f"exp-unknown-folder:{folder}",
                    event_type=UNKNOWN_FOLDER_EVENT,
                    payload={"folder": folder, "receipt_files": len(files)},
                )
            )
        if files:
            digest = hashlib.sha256(
                "|".join(f"{f.name}:{_sha256(f)}" for f in files).encode()
            ).hexdigest()
            approvals.append(
                ApprovalSpec(
                    key=f"expunknown:{folder}:{digest[:16]}",
                    action_type="expenses.unknown_person_folder",
                    params={
                        "folder": folder,
                        "path": str(drop / folder),
                        "files": [f.name for f in files],
                    },
                    reason=f"{len(files)} receipt file(s) sit in a folder intake will "
                    "never process; add the person to [expenses].persons or move the "
                    "files to a configured person, then approve to acknowledge",
                )
            )

    summary = (
        f"expenses intake: filed {filed}, already-filed {already}, duplicate {dup}, "
        f"needs-attribution {carded}"
    )
    if unknown:
        summary += f", unknown-folder {len(unknown)}"
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


# ---- job: extract -----------------------------------------------------------


RECEIPT_EXTRACT_JOB = "receipt_extract"


def _extractor_kind(ctx: JobContext) -> str:
    import os

    return ctx.params.get("extractor") or os.environ.get("ENGINE_EXTRACTOR") or "claude"


def _extractor(ctx: JobContext):
    return build_extractor(_extractor_kind(ctx), ctx, job_type=RECEIPT_EXTRACT_JOB)


def _extract_pending(ctx: JobContext) -> list[dict]:
    landed = _landed_by_sha(ctx)
    proposed = set(_proposals_by_sha(ctx)) | _reference_shas(ctx)
    return [p for sha, p in sorted(landed.items()) if sha not in proposed]


def _extract_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expextract")
    key.value("pending", sorted(str(p.get("sha256")) for p in _extract_pending(ctx)))
    key.param("extractor")
    key.param("filing_dir")
    key.config("expenses")
    key.config("books.cost_object")  # the project tag in file names (#340)
    # Row 7.10: the receipt pass runs through the same gateway extractor, so
    # the resolved tier rides the key (docs/run-keys.md).
    key.config("llm")
    key.value("llm_tier", resolved_tier(ctx, _extractor_kind(ctx), RECEIPT_EXTRACT_JOB))
    return key.digest()


def _extract_run(ctx: JobContext) -> JobOutput:
    pending = _extract_pending(ctx)
    if not pending:
        return JobOutput(status="ok", summary="expenses extract: nothing pending")

    extractor = _extractor(ctx)
    filing = _filing_dir(ctx)
    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    proposed = referenced = failed = 0

    for payload in pending:
        sha = str(payload["sha256"])
        path = Path(str(payload.get("filed_to", "")))
        if not path.is_file():
            anomalies.append(
                Anomaly(
                    code="expenses.receipt_missing",
                    detail=f"{payload.get('file')} landed but is not at {path}",
                )
            )
            continue
        try:
            doc = extractor.extract(path)
        except ExtractionError as exc:
            failed += 1
            anomalies.append(Anomaly(code="expenses.extract_failed", detail=f"{path.name}: {exc}"))
            continue

        if doc.doc_type == "reference":
            month = str(payload.get("month") or _tenant_month(ctx))
            ref_dir = filing / month / "_reference"
            try:
                dest = _file_copy(path, ref_dir, guard=ctx.guard, shadow=ctx.shadow)
            except (CannotVerify, CopyMismatch) as exc:
                anomalies.append(_placement_anomaly("expenses", path.name, exc))
                continue  # the only copy stays put until the move is verified
            if ctx.shadow:
                # Nothing was written, so nothing is claimed (03-F8): a
                # recorded reference event would hide the receipt from every
                # later real run. Mirrors the intake loop's shadow guard.
                actions.append(f"would file {path.name} -> _reference/ ({ref_dir})")
                continue
            # The unlink stands on a read-back-verified copy (03-F4).
            ctx.guard.check_write(path)
            path.unlink()
            referenced += 1
            actions.append(f"reference material: {path.name} -> _reference/")
            events.append(
                EventSpec(
                    key=f"expense-ref:{sha}",
                    event_type=REFERENCE_EVENT,
                    payload={"sha256": sha, "file": path.name, "filed_to": str(dest)},
                )
            )
            continue

        flags: list[str] = []
        amount_cents: int | None = None
        if doc.amount is not None:
            amount_cents = amount_to_cents(doc.amount)
        tag_cents = parse_amount_tag(path.name)
        if tag_cents is not None and amount_cents is not None and tag_cents != amount_cents:
            flags.append(
                f"amount-in-filename ${tag_cents / 100:,.2f} disagrees with "
                f"proposal ${amount_cents / 100:,.2f}"
            )
        if amount_cents is None and tag_cents is not None:
            amount_cents = tag_cents
            flags.append("amount taken from filename; no amount extracted")
        if amount_cents is None:
            flags.append("no amount found; the review card must resolve this line")
        file_tag = parse_project_tag(path.name, ctx.tenant.books.cost_object)
        if file_tag and file_tag != parse_project_tag(
            str(payload.get("project", "")), ctx.tenant.books.cost_object
        ):
            flags.append(f"filename project tag {file_tag} vs attributed {payload.get('project')}")
        category = normalize_category(doc.category, path.name)

        proposed += 1
        actions.append(f"proposed {path.name}: {doc.vendor_name or '?'} {category or '?'}")
        events.append(
            EventSpec(
                key=f"expense-extract:{sha}",
                event_type=PROPOSED_EVENT,
                payload={
                    "sha256": sha,
                    "file": path.name,
                    "vendor": doc.vendor_name or "",
                    "expense_date": doc.invoice_date or "",
                    "amount_cents": amount_cents,
                    "category": category,
                    "confidence": doc.confidence,
                    "flags": flags,
                },
            )
        )

    return JobOutput(
        status="ok",
        summary=f"expenses extract: proposed {proposed}, reference {referenced}, failed {failed}",
        actions=actions,
        events=events,
        anomalies=anomalies,
    )


# ---- job: report ------------------------------------------------------------


def _unconsumed(ctx: JobContext) -> list[dict]:
    """Landed receipts no manifest has consumed and no reference event
    reclassified, joined to their proposals when one exists. A door receipt
    whose amount the person said after the photo carries it as
    ``claim_name``, the name it would have had with the claim in it."""
    consumed = _consumed_shas(ctx) | _reference_shas(ctx)
    proposals = _proposals_by_sha(ctx)
    log = ctx.ledger.read_event_log()
    said = amounts_said(log)
    dated = records.days(log)
    out = []
    for sha, landed in sorted(_landed_by_sha(ctx).items()):
        if sha in consumed:
            continue
        item = dict(landed)
        item.update(proposals.get(sha, {}))
        item["sha256"] = sha
        name = str(landed.get("file", ""))
        if sha in dated:
            # A receipt sent later is filed by its own day (t-catch-up):
            # its month, its line, and its rate are that day's.
            name = records.dated_name(name, dated[sha])
            item["month"] = dated[sha][:7]
        elif records.UNDATED in name:
            item["month"] = ""  # in no month until its day is known
        if sha in said:
            item["claim_name"] = claim_name(name, said[sha])
        elif name != landed.get("file"):
            item["claim_name"] = name
        out.append(item)
    return out


def _drive_items(ctx: JobContext, month: str, person: str = "") -> list[dict]:
    """The month's ministry drives not yet in a report, each a line priced at
    the rate in force on its own day (the owner, 2026-10-14). A drive the
    person called personal, or hasn't spoken for, never becomes money."""
    from ...institution import mileage_rate

    said = {str(e["payload"].get("drive")): e["payload"] for e in _events_of(ctx, PURPOSE_EVENT)}
    consumed = _consumed_shas(ctx)
    listed = list(ctx.tenant.expenses.category_accounts)
    category = cats.canonical("Mileage", listed) or cats.canonical("Travel", listed) or ""
    out = []
    for e in _events_of(ctx, DRIVE_EVENT):
        drive = e["payload"]
        day = str(drive.get("start", ""))[:10]
        word = said.get(drive_id(drive), {})
        sha = records.drive_sha(drive_id(drive))
        if day[:7] != month or not word.get("ministry") or sha in consumed:
            continue
        if person and drive.get("person") != person:
            continue
        miles = Decimal(str(drive.get("miles", "0")))
        rate, rate_from = mileage_rate(ctx.tenant.institution, date.fromisoformat(day))
        words = str(word.get("said", "")).replace(";", ",")
        out.append(
            {
                "sha256": sha,
                "file": f"{day} drive, {miles} miles",
                "drive": True,
                "person": str(drive.get("person", "")),
                "month": month,
                "expense_date": day,
                "miles": str(miles),
                "rate": str(rate),
                "rate_from": rate_from,
                "note": words,
                "amount_cents": int(records.drive_dollars(miles, rate) * 100),
                "category": category,
                "project": "",
                "filed_to": "",
                "flags": [],
            }
        )
    return out


def _bank_items(ctx: JobContext, month: str, person: str = "") -> list[dict]:
    """The month's statement lines and school payments that are spending
    (t-statements, the owner's decisions of 2026-10-14), each a line of the
    person's card: what the person paid for the ministry, owed back, and
    what the ministry paid itself, on the card for the reviewer's yes but
    never owed. A line still waiting on its receipt or its purpose stays off
    the card until it has one; money that only moved is never a line."""
    listed = list(ctx.tenant.expenses.category_accounts)
    log = ctx.ledger.read_event_log()
    out = []
    for ln in statements.month_lines(log, month, reported=_consumed_shas(ctx), categories=listed):
        if ln["role"] not in statements.SPENDING or ln["reported"]:
            continue
        if ln["waiting_on"] == statements.WAITING or (person and ln["person"] != person):
            continue
        item = {
            "sha256": ln["sha256"],
            "file": f"{ln['day']} {ln['words']}",
            "bank": True,
            "paid_by": "ministry" if ln["role"] == statements.MINISTRY_PAID else "person",
            "person": ln["person"],
            "month": month,
            "expense_date": ln["day"],
            "vendor": ln["words"],
            "note": ln["words"],
            "amount_cents": ln["cents"],
            "category": ln["category"],
            "project": "",
            "filed_to": ln["receipt_file"],
            "flags": [],
        }
        if ln.get("school") and ln.get("said_currency"):
            # Priced the one way a foreign amount is: at its day's rate.
            item |= {"claim_name": ln["claim_name"], "amount_cents": None}
        out.append(item)
    return out


def _with_statements(ctx: JobContext, month: str, person: str, mine: list[dict]) -> list[dict]:
    """A person's month with its statement lines: a receipt standing behind
    a line rides that line, never a line of its own; drives and lines join."""
    behind = statements.attached(ctx.ledger.read_event_log())
    return [
        *(i for i in mine if str(i.get("sha256")) not in behind),
        *_drive_items(ctx, month, person),
        *_bank_items(ctx, month, person),
    ]


def _is_door_receipt(name: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}_receipt_", name))


_TAG = re.compile(r"^(?:\$\d+\.\d{2}|[A-Z]{3}\d+\.\d{2})$")


def amounts_said(events: list[dict], kind: str = AMOUNT_EVENT) -> dict[str, str]:
    """Each door receipt's latest amount tag of ``kind`` ("$43.00",
    "NPR4300.00"): the person's words (``AMOUNT_EVENT``) or a photo's
    reading (``READ_EVENT``)."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"]["tag"])
        for e in events
        if e.get("event_type") == kind and e.get("payload", {}).get("sha256")
    }


def claim_name(name: str, tag: str) -> str:
    """A door receipt's name as if the claim had been said with it, for the
    pricing that reads claims from names."""
    return f"{name[:10]}_receipt_{tag}{Path(name).suffix}"


def _name_of(item: dict) -> str:
    return str(item.get("claim_name") or item.get("file", ""))


def _fx_inputs(ctx: JobContext, items: list[dict]) -> tuple[list[str], str]:
    """What a foreign receipt's price reads, for a run key: the rates
    already recorded for these receipts, and today's date while any rate is
    the time while any rate is still pending (so a pending rate is asked
    for again on the next run, never skipped as an idempotent re-run)."""
    rates = fx.stored(ctx.ledger.read_event_log())
    used, pending = [], False
    for i in items:
        claim = parse_foreign_claim(_name_of(i))
        if claim is None:
            continue
        day, _amount, currency = claim
        if (currency, day) in rates:
            used.append(fx.rate_key(currency, day))
        else:
            pending = True
    asked = datetime.now(UTC).isoformat() if pending else ""
    return sorted(set(used)), asked


def _price_claims(ctx: JobContext, items: list[dict]) -> tuple[list[dict], list[EventSpec]]:
    """Price door receipts from the person's own words (alpha step 3). A
    claim in dollars is the amount when nothing else priced the receipt; a
    claim in another currency is always priced at the rate on the
    receipt's own date (fx.priced), recorded once and kept on the line. With
    no rate yet, the line stays unpriced and says so; nothing is guessed."""
    rates = fx.stored(ctx.ledger.read_event_log())
    events: list[EventSpec] = []
    for want in sorted({w for i in items if (w := fx.needed(_name_of(i)))}):
        if want in rates:
            continue
        rate = fx.fetch(*want)
        if rate is not None:
            rates[want] = rate
            events.append(
                EventSpec(key=rate.key(), event_type=fx.RATE_EVENT, payload=rate.payload())
            )
    out = []
    for item in items:
        name = _name_of(item)
        if not _is_door_receipt(name):
            out.append(item)
            continue
        item = dict(item)
        flags = [str(f) for f in item.get("flags", [])]
        fields = fx.priced(name, rates)
        if not fields:
            said = parse_amount_tag(name)
            if not item.get("amount_cents") and said is not None:
                item["amount_cents"] = said
                flags.append("amount as the person said it")
        else:
            read = item.get("amount_cents")
            pending = fields.pop("rate_pending", False)
            item.update(fields)
            currency, day = fields["said_currency"], fields["fx_date"]
            if pending:
                flags.append(
                    f"{currency} {Decimal(fields['said_amount']):,.2f}: rate pending for {day}"
                )
            elif read and int(read) != fields["amount_cents"]:
                flags.append(f"the photo read {int(read) / 100:,.2f}; the person said {currency}")
        item["flags"] = flags
        out.append(item)
    return out, events


def _line_words(i: dict, label: str = "", *, funds: int = 2) -> str:
    """One line of a review card, a foreign amount shown as said with its rate.
    A door receipt reads in a reviewer's words: its day, what it was, its
    category ("Uncategorized" when nobody has placed it), and its fund when
    the card's lines span more than one (``funds``; the 10/14 practice run: a
    church with one fund read "General fund" on every line). A drive reads
    its miles, what it was for, and its rate per mile."""
    if i.get("bank"):
        words = f"{i['expense_date']}: ${int(i['amount_cents']) / 100:,.2f}"
        if i.get("said_currency"):
            words += (
                f" ({i['said_currency']} {Decimal(i['said_amount']):,.2f} at "
                f"{Decimal(i['fx_rate']):,.2f} per dollar on {i['fx_date']})"
            )
        paid = f", {statements.MINISTRY_PAID}" if i.get("paid_by") == "ministry" else ""
        return f"{words}, {i['note']}, {i.get('category') or cats.UNCATEGORIZED}{paid}"
    if i.get("drive"):
        cents = f"{Decimal(i['rate']) * 100:f}".rstrip("0").rstrip(".")
        return (
            f"{i['expense_date']}: {i['miles']} miles, {i['note']}, "
            f"${int(i['amount_cents']) / 100:,.2f} at {cents} cents a mile"
        )
    if _is_door_receipt(str(i["file"])):
        words = f"{_name_of(i)[:10]}: ${int(i['amount_cents']) / 100:,.2f}"
        if i.get("said_currency"):
            words += (
                f" ({i['said_currency']} {Decimal(i['said_amount']):,.2f} at "
                f"{Decimal(i['fx_rate']):,.2f} per dollar on {i['fx_date']})"
            )
        where = f", {i['project']} {label}".rstrip() if i.get("project") and funds > 1 else ""
        return f"{words}, {i.get('category') or cats.UNCATEGORIZED}{where}"
    words = f"{i['file']} ${int(i['amount_cents']) / 100:,.2f}"
    if i.get("said_currency"):
        words += (
            f" ({i['said_currency']} {Decimal(i['said_amount']):,.2f} at "
            f"{Decimal(i['fx_rate']):,.2f} per dollar on {i['fx_date']})"
        )
    return f"{words} {i.get('category') or '?'} ({i.get('project') or '?'})"


def _report_content_hash(items: list[dict]) -> str:
    parts = [
        f"{i['sha256']}:{i.get('amount_cents')}:{i.get('category')}:{i.get('project')}"
        + (f":{i['claim_name']}" if i.get("claim_name") else "")
        + (f":{i['note']}" if i.get("drive") or i.get("bank") else "")  # words on its line
        + (f":{i['paid_by']}" if i.get("bank") else "")
        for i in items
    ]
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()


def _report_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expreport")
    key.add("person", str(key.param("person") or ""))
    # price:<sha16> directives are run inputs (an owner-supplied amount);
    # each is declared by name, and the scan itself is the wildcard read.
    for name in sorted(k for k, _v in ctx.params.items() if k.startswith("price:")):
        key.param(name)
    # Issue #135: the key hashes the month run() will actually use. Hashing
    # the unresolved param ('') let September's default-month survey replay
    # August's result verbatim; a new month is a new run by definition.
    key.add("month", str(key.param("month") or _tenant_month(ctx)))
    key.add("content", _report_content_hash(_unconsumed(ctx)))
    bank = _bank_items(ctx, str(key.param("month") or _tenant_month(ctx)))
    if bank:  # a tenant with no statements keeps the key it always had
        key.add("bank", _report_content_hash(bank))
    rates, pending_day = _fx_inputs(ctx, _unconsumed(ctx))
    key.value("fx", rates)
    key.value("placed", sorted(_categories_by_sha(ctx).items()))
    key.add("fx_pending_day", pending_day)
    key.value("approved", sorted(c["idempotency_key"] for c in _approved_cards(ctx, REVIEW_CARD)))
    # 03-F6: a stranded report (rows committed, nothing recorded) is a run
    # input; once recovered the key moves on and the next run builds.
    key.value("stranded", [r["id"] for r in _stranded_reports(ctx)])
    rules = _rules_in_force_key(ctx)
    if rules is not None:  # a ministry with no rules in force keeps the key it had
        key.value("rules", rules)
        key.config("fiscal")
    key.param("filing_dir")
    key.config("expenses", "identity")  # persons, prefix, the metadata legal name
    key.config("books.cost_object")  # the fund or project label on a card line
    return key.digest()


# ---- the reimbursement confirm card ----------------------------------------


def _confirm_card_state(ctx: JobContext, report_id: int) -> tuple[str, int | None]:
    """(status, card_id) of the newest confirm card for this report, or
    ("none", None). Newest wins: a rejected card followed by a fresh ask
    means the owner is asking again (the close lock pattern, issue #161)."""
    rows = ctx.ledger.conn.execute(
        "SELECT id, params_json, status FROM approval_queue WHERE tenant = ? "
        "AND action_type = ? ORDER BY id DESC",
        (ctx.tenant_slug, CONFIRM_CARD),
    ).fetchall()
    for row in rows:
        try:
            params = json.loads(row["params_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if str(params.get("report_id", "")) == str(report_id):
            return str(row["status"]), int(row["id"])
    return "none", None


def _confirm_spec(ctx: JobContext, report: dict, *, reason: str, **extra: str) -> ApprovalSpec:
    """The confirm card for a report. ``expconfirm:{report_id}`` is an
    identity key, so the runner's stable-subject dedup (#143) would swallow
    every re-park against a rejected row (honesty audit 2026-09-03, 03-F7,
    the 2026-09-01 shape): when the newest card is rejected the ask rides a
    fresh key naming the card it supersedes."""
    report_id = int(report["id"])
    state, card_id = _confirm_card_state(ctx, report_id)
    key = f"expconfirm:{report_id}"
    if state == "rejected":
        key = f"{key}:reask-{card_id}"
    return ApprovalSpec(
        key=key,
        action_type=CONFIRM_CARD,
        params={
            "report_id": str(report_id),
            "person": str(report["person"]),
            "month": str(report["month"]),
            "total_cents": str(report["total_cents"]),
            "channel": _channel_for(ctx, str(report["person"])),
            **extra,
        },
        reason=reason,
    )


CONFIRM_REASON = (
    "approve once the reimbursement has been paid; the engine then "
    "records the split so the bank-feed line arrives pre-matched"
)


def _built_report_keys(ctx: JobContext) -> set[str]:
    return {
        str(e.get("payload", {}).get("report_key", ""))
        for e in _events_of(ctx, BUILT_EVENT)
        if e.get("payload", {}).get("report_key")
    }


def _stranded_reports(ctx: JobContext) -> list[dict]:
    """Open reports whose rows committed but whose ``expense.report_built``
    event never landed: the #135 crash-mid-move shape (honesty audit
    2026-09-03, 03-F6). Rows commit before the receipts move by design, so
    a crash there leaves the report with no confirm card and no event, and
    ``_unconsumed`` (which reads the rows) answers "nothing" forever."""
    built = _built_report_keys(ctx)
    return [r for r in _reports(ctx, STATUS_OPEN) if r["idempotency_key"] not in built]


def _recover_stranded(ctx: JobContext) -> JobOutput | None:
    """Re-derive the confirm card and the built event from the Open row,
    and finish any half-done ``_filed/`` move. Idempotent: once the event
    exists the report is no longer stranded."""
    stranded = _stranded_reports(ctx)
    if not stranded:
        return None
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    filing = _filing_dir(ctx)
    landed = _landed_by_sha(ctx)
    for report in stranded:
        report_key = str(report["idempotency_key"])
        content16 = report_key.rsplit(":", 1)[-1]
        lines = _lines_for(ctx, int(report["id"]))
        loose: list[Path] = []
        for sha in sorted({str(line["receipt_sha256"]) for line in lines}):
            src = Path(str(landed.get(sha, {}).get("filed_to", "")))
            if src.name and src.is_file():
                loose.append(src)
        if ctx.shadow:
            actions.append(
                f"would recover report #{report['id']}: confirm card, built event, "
                f"{len(loose)} loose receipt(s) -> _filed/"
            )
            continue
        filed_dir = filing / str(report["month"]) / "_filed" / content16
        for src in loose:
            dest = filed_dir / src.name
            ctx.guard.check_write(dest)
            filed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)
        anomalies.append(
            Anomaly(
                code="expenses.report_recovered",
                detail=f"report #{report['id']} ({report['person']} {report['month']}) was "
                "Open with no confirm card and no report_built event (a run failed after "
                f"its rows committed); re-derived both and moved {len(loose)} loose "
                "receipt(s) to _filed/",
            )
        )
        actions.append(
            f"recovered report #{report['id']}: {len(loose)} receipt(s) -> _filed/, "
            "confirm card parked"
        )
        events.append(
            EventSpec(
                key=f"expreport-built:{content16}",
                event_type=BUILT_EVENT,
                payload={
                    "report_id": report["id"],
                    "report_key": report_key,
                    "person": report["person"],
                    "month": report["month"],
                    "total_cents": report["total_cents"],
                    "lines": len(lines),
                    "report_path": str(report.get("report_path") or ""),
                    "recovered": True,
                },
            )
        )
        approvals.append(_confirm_spec(ctx, report, reason=CONFIRM_REASON))
    return JobOutput(
        status="ok",
        summary=f"recovered {len(stranded)} stranded report(s)",
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


def _with_recovery(recovery: JobOutput | None, output: JobOutput) -> JobOutput:
    if recovery is None:
        return output
    return JobOutput(
        status="error" if "error" in (recovery.status, output.status) else "ok",
        summary=f"{output.summary}; {recovery.summary}",
        actions=[*recovery.actions, *output.actions],
        events=[*recovery.events, *output.events],
        approvals=[*recovery.approvals, *output.approvals],
        anomalies=[*recovery.anomalies, *output.anomalies],
        rubric=output.rubric,
    )


def _render_workbook(path: Path, manifest: ReportManifest, legal_name: str) -> None:
    """The report xlsx: hardcoded values, tenant metadata (invariant 10)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Expense Report"
    ws.append([f"{legal_name} — Expense Report {manifest.month}"])
    ws.append([f"Person: {manifest.person}"])
    ws.append([])
    ws.append(["Receipt", "Vendor", "Date", "Amount", "Category", "Project", "Note"])
    for line in manifest.lines:
        ws.append(
            [
                line.receipt_file,
                line.vendor,
                line.expense_date,
                line.amount_cents / 100,
                line.category,
                line.project,
                line.note,
            ]
        )
    ws.append([])
    ws.append(["Total", "", "", manifest.total_cents / 100])
    props = wb.properties
    props.creator = legal_name
    props.lastModifiedBy = legal_name
    wb.save(path)


def _parse_split(value: str) -> list[tuple[int, str, str]]:
    """Parse an owner split directive: ``amount:category[:note]|...``.

    Amounts are dollars, Decimal-parsed to cents (invariant 2's sibling:
    code computes, exactly). Raises ValueError on any malformed part.
    """
    from decimal import Decimal, InvalidOperation

    parts: list[tuple[int, str, str]] = []
    for chunk in str(value).split("|"):
        fields = chunk.strip().split(":", 2)
        if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
            raise ValueError(f"split part {chunk!r} is not amount:category[:note]")
        try:
            cents = int((Decimal(fields[0].strip()) * 100).to_integral_value())
        except InvalidOperation as exc:
            raise ValueError(f"split amount {fields[0]!r} is not a number") from exc
        note = fields[2].strip() if len(fields) == 3 else ""
        parts.append((cents, fields[1].strip(), note))
    if len(parts) < 2:
        raise ValueError("a split needs at least two parts")
    return parts


def _parse_price(value: str) -> tuple[int, str, str]:
    """Parse an owner price directive: ``amount:category[:note]``. One
    receipt, one amount, Decimal-parsed to cents, never zero or negative."""
    from decimal import Decimal, InvalidOperation

    fields = str(value).strip().split(":", 2)
    if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
        raise ValueError(f"price {value!r} is not amount:category[:note]")
    try:
        cents = int((Decimal(fields[0].strip()) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise ValueError(f"price amount {fields[0]!r} is not a number") from exc
    if cents <= 0:
        raise ValueError(f"price amount {fields[0]!r} must be positive")
    note = fields[2].strip() if len(fields) == 3 else ""
    return cents, fields[1].strip(), note


def _price_directives(ctx: JobContext) -> dict[str, str]:
    """``price:<sha256[:16]>`` params on the report run: the owner pricing
    a receipt the extractor could not read (a two-receipts-on-one-scan
    page, a faded total) or correcting one it read wrong. The value is
    recorded as ``expense.owner_priced`` so every later run, including the
    approval that builds the report, carries it; a corrected receipt keeps
    its extractor amount in the event as ``previous_amount_cents``."""
    return {
        k.split(":", 1)[1]: str(v)
        for k, v in ctx.params.items()
        if k.startswith("price:") and k.split(":", 1)[1]
    }


def _split_directives(card: dict) -> dict[str, str]:
    """``split:<sha256[:16]>`` params from the approved review card (#110)."""
    return {
        k.split(":", 1)[1]: str(v)
        for k, v in (card.get("params") or {}).items()
        if k.startswith("split:") and k.split(":", 1)[1]
    }


def _price_items(
    items: list[dict], directives: dict[str, str], person: str, month: str
) -> tuple[list[dict], list[EventSpec], JobOutput | None]:
    """Lay the run's price directives over the person's unconsumed items.
    Owner values enter only through the directive; a malformed value or a
    prefix naming no receipt in this report refuses the whole build, and
    nothing is recorded (the split directive's rule, invariant 2)."""
    if not directives:
        return items, [], None
    pending = dict(directives)
    out: list[dict] = []
    events: list[EventSpec] = []
    for i in items:
        sha = str(i["sha256"])
        directive = pending.pop(sha[:16], None)
        if directive is None:
            out.append(i)
            continue
        try:
            cents, category, note = _parse_price(directive)
        except ValueError as exc:
            return (
                items,
                [],
                JobOutput(
                    status="ok",
                    summary=f"expenses report: price for {i['file']} is invalid; nothing built",
                    anomalies=[
                        Anomaly(code="expenses.price_invalid", detail=f"{i['file']}: {exc}")
                    ],
                ),
            )
        price = {
            "sha256": sha,
            "file": str(i.get("file") or ""),
            "amount_cents": cents,
            "category": category,
            "note": note,
            "previous_amount_cents": i.get("amount_cents"),
            "person": person,
            "month": month,
        }
        events.append(
            EventSpec(
                key=f"exppriced:{sha}:{cents}:{category}", event_type=PRICED_EVENT, payload=price
            )
        )
        out.append(_apply_owner_price(i, price))
    if pending:
        unknown = ", ".join(sorted(pending))
        return (
            items,
            [],
            JobOutput(
                status="ok",
                summary="expenses report: price directive names no receipt for "
                f"{person} {month}; nothing built",
                anomalies=[
                    Anomaly(
                        code="expenses.price_invalid",
                        detail=f"price sha prefix(es) {unknown} match no unconsumed receipt",
                    )
                ],
            ),
        )
    return out, events, None


_FX_FIELDS = ("said_amount", "said_currency", "fx_rate", "fx_date", "fx_source")


def _pending_review_keys(ctx: JobContext, person: str, month: str) -> list[str]:
    """The keys of this person's month's review cards still waiting, so a
    new card for the same month retires the old one instead of piling up."""
    prefix = f"apr:{ctx.tenant_slug}:expenses:{REVIEW_CARD}:"
    rows = ctx.ledger.conn.execute(
        "SELECT idempotency_key FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'pending'",
        (ctx.tenant_slug, REVIEW_CARD),
    ).fetchall()
    keys = [str(r[0])[len(prefix) :] for r in rows if str(r[0]).startswith(prefix)]
    return sorted(k for k in keys if k.startswith(f"expreport:{person}:{month}:"))


def _warnings(items: list[dict]) -> list[str]:
    """What a reviewer should know before a yes: each line's own flags, and
    scans that look like the same receipt twice."""
    warnings = [str(f) for i in items for f in i.get("flags", [])]
    dupes = duplicate_groups(
        [
            {
                "file": i.get("file"),
                "amount": i.get("amount_cents"),
                "date": i.get("expense_date"),
            }
            for i in items
        ]
    )
    for group in dupes:
        warnings.append("possible duplicate scans: " + ", ".join(sorted(group)))
    return warnings


def _already_words(ctx: JobContext, person: str, month: str, approved: list[dict], yes) -> str:
    """One line naming what the person's month already holds a yes for:
    "Already approved Oct 13: 2 receipts, $42.31". Receipts already in a
    report count on the day their report was built."""
    zone = ZoneInfo(ctx.tenant.identity.timezone)
    days: dict[str, list[int]] = {}

    def add(on: str, cents: int) -> None:
        when = datetime.fromisoformat(on).astimezone(zone).date().isoformat()
        days.setdefault(when, []).append(cents)

    for i in approved:
        add(yes[str(i["sha256"])].on, int(i["amount_cents"]))
    for r in ctx.ledger.conn.execute(
        "SELECT l.amount_cents, r.created_at FROM expense_line l "
        "JOIN expense_report r ON r.id = l.report_id "
        "WHERE r.tenant = ? AND r.person = ? AND r.month = ? AND r.shadow = 0",
        (ctx.tenant_slug, person, month),
    ).fetchall():
        add(str(r[1]), int(r[0]))
    said = []
    for when, cents in sorted(days.items()):
        day = date.fromisoformat(when)
        n = len(cents)
        said.append(
            f"{day:%b} {day.day}: {n} receipt{'s' if n != 1 else ''}, ${sum(cents) / 100:,.2f}"
        )
    return "Already approved " + "; ".join(said) if said else ""


def _legacy_yes(ctx: JobContext, person: str, month: str, items: list[dict]) -> dict | None:
    """An approved card from before cards named their receipts: it covers the
    month's receipts when its key carries their content, as it always did."""
    key = f"expreport:{person}:{month}:{_report_content_hash(items)[:16]}"
    return next(
        (
            c
            for c in _approved_cards(ctx, REVIEW_CARD)
            if c["idempotency_key"].endswith(key) and not records.card_receipts(c["params"])
        ),
        None,
    )


def _rules_in_force_key(ctx: JobContext) -> list[str] | None:
    """The fingerprints of the ministry's rules in force, or None when it has
    none (its run keys and cards stay exactly as they were)."""
    authority = ctx.authority
    if authority is None or (authority.rules is None and not authority.rules_error):
        return None
    if authority.rules_error:
        return [f"unreadable:{authority.rules_error}"]
    shas = sorted(r.sha for r in rules_in_force(ctx.ledger, ctx.tenant_slug, authority.rules))
    return shas or None


def _spent_words(i: dict) -> str:
    """A line the way a reviewer names it: what it was and its day ("Meals,
    Oct 12"). A door receipt is named by its category, as on the card."""
    door = "file" in i and _is_door_receipt(str(i["file"]))
    what = i.get("category") if door else i.get("note") or i.get("vendor")
    what = " ".join(str(what or "A receipt").split())
    what = what if len(what) <= 40 else what[:39].rstrip() + "…"
    try:
        day = date.fromisoformat((_name_of(i) if door else str(i.get("expense_date", "")))[:10])
    except ValueError:
        return what
    return f"{what}, {day:%b} {day.day}"


def _policy_findings(
    ctx: JobContext, person: str, month: str, items: list[dict]
) -> tuple[str, bool] | None:
    """What the ministry's rules in force say about this person's month (the
    whole month: this card's lines, earlier yeses, and lines already in a
    report), in plain words with each rule's page; and whether a rule asks
    for the person's own Face ID. None when no rule is in force."""
    authority = ctx.authority
    if _rules_in_force_key(ctx) is None:
        return None
    if authority.rules_error:
        return f"The ministry's rules could not be checked: {authority.rules_error}", False
    who = unit_of(person)
    held = authority.policy.people.get(who)
    roles = held.roles if held else ()
    spent = [
        Spent(
            key=str(i["sha256"]),
            words=_spent_words(i),
            kind=str(i.get("category") or ""),
            cents=int(i["amount_cents"]),
            receipt=bool(i.get("drive")) or not i.get("bank") or bool(i.get("filed_to")),
            bank=bool(i.get("bank")),
        )
        for i in items
        if i.get("amount_cents") is not None
    ]
    start = ctx.tenant.fiscal.year_start_month
    year, mon = int(month[:4]), int(month[5:7])
    first = f"{year if mon >= start else year - 1}-{start:02d}"
    year_before: dict[str, int] = {}
    for r in ctx.ledger.conn.execute(
        "SELECT r.month, l.receipt_sha256, l.vendor, l.note, l.expense_date, l.amount_cents, "
        "l.category FROM expense_line l JOIN expense_report r ON r.id = l.report_id "
        "WHERE r.tenant = ? AND r.person = ? AND r.shadow = 0 AND r.month >= ? AND r.month <= ?",
        (ctx.tenant_slug, person, first, month),
    ).fetchall():
        if r["month"] < month:
            year_before[r["category"]] = year_before.get(r["category"], 0) + int(r["amount_cents"])
        else:
            spent.append(
                Spent(
                    str(r["receipt_sha256"]),
                    _spent_words(dict(r)),
                    str(r["category"]),
                    int(r["amount_cents"]),
                    receipt=True,
                )
            )
    in_force = rules_in_force(ctx.ledger, ctx.tenant_slug, authority.rules)
    found = check_month(in_force, spent, person=who, roles=roles, year_before=year_before)
    return "; ".join(f.says for f in found) or "none", any(f.face_id for f in found)


def _review_card(
    ctx: JobContext, person: str, month: str, items: list[dict], waiting: list[dict], yes
) -> JobOutput:
    """The card for what in a person's month still waits on a yes: every
    line, what the yes commits, what it leaves alone, and what only moved."""
    approved = [i for i in items if str(i["sha256"]) in yes]
    card_key = f"expreport:{person}:{month}:{_report_content_hash(waiting)[:16]}"
    rules = _rules_in_force_key(ctx)
    if rules is not None:  # new rules in force are a new card (the old one retires)
        card_key += ":r" + hashlib.sha256("".join(rules).encode()).hexdigest()[:8]
    label = ctx.tenant.books.cost_object.label
    funds = len({i.get("project") for i in items if i.get("project")})
    lines_text = "; ".join(_line_words(i, label, funds=funds) for i in waiting)
    older = [k for k in _pending_review_keys(ctx, person, month) if k != card_key]
    shas = {str(i["sha256"]) for i in waiting}
    params = {
        "person": person,
        "month": month,
        # What the yes commits: the ministry owes back only what the
        # person paid; a line the ministry paid itself is on the card
        # for the yes, never owed (t-statements).
        "total_cents": str(
            sum(int(i["amount_cents"]) for i in waiting if i.get("paid_by") != "ministry")
        ),
        "lines": lines_text,
        "warnings": "; ".join(_warnings(waiting)) or "none",
        "receipts": ",".join(sorted(shas)),
    }
    already = _already_words(ctx, person, month, approved, yes)
    if already:
        params["already"] = already
    policy = _policy_findings(ctx, person, month, items)
    if policy is not None:
        params["policy"], face_id = policy
        if face_id:
            params["policy_face_id"] = "true"
    moved = statements.moved_words(
        [
            ln
            for ln in statements.month_lines(ctx.ledger.read_event_log(), month)
            if ln["person"] == person
        ]
    )
    if moved:
        params["moved"] = moved
    return JobOutput(
        status="ok",
        summary=f"expenses report: {len(waiting)} line(s) for {person} {month} awaiting review",
        approvals=[
            ApprovalSpec(
                key=card_key,
                action_type=REVIEW_CARD,
                params=params,
                reason="only owner-approved values enter the workbook or the ledger (invariant 2)",
                supersedes_keys=older,
            )
        ],
    )


def _build_report(
    ctx: JobContext,
    person: str,
    month: str,
    items: list[dict],
    changed: frozenset[str] = frozenset(),
) -> JobOutput:
    events: list[EventSpec] = []
    actions: list[str] = []
    filing = _filing_dir(ctx)

    unpriced = [i for i in items if not i.get("amount_cents")]
    if unpriced:
        names = ", ".join(str(i.get("file")) for i in unpriced)
        return JobOutput(
            status="ok",
            summary=f"expenses report: {len(unpriced)} receipt(s) lack an amount "
            f"({names}); resolve extraction first",
        )

    # A yes is to the receipts its card named (records, 2026-10-14): a late
    # receipt waits on a card of its own, and the month builds once every
    # receipt in it holds a yes its facts still match.
    yes_cards = _approved_cards(ctx, REVIEW_CARD)
    yes = records.yeses(yes_cards, ctx.ledger.read_event_log(), changed)
    legacy = _legacy_yes(ctx, person, month, items)
    waiting = [] if legacy else [i for i in items if str(i["sha256"]) not in yes]
    if waiting:
        return _review_card(ctx, person, month, items, waiting, yes)

    items = [i for i in items if i.get("paid_by") != "ministry"]  # yes'd, never owed
    if not items:
        return JobOutput(
            status="ok",
            summary=f"expenses report: {person} {month} approved; the ministry owes nothing back",
        )
    content = _report_content_hash(items)
    total = sum(int(i["amount_cents"]) for i in items)

    if ctx.shadow:
        return JobOutput(
            status="ok",
            summary=f"expenses report: would build {person} {month} "
            f"(${total / 100:,.2f}, {len(items)} lines)",
        )

    # Owner-directed splits (issue #110, the Kala/hotel-folio shape): the
    # approved card may carry split:<sha16> directives. Owner values enter
    # only through the card; code enforces the arithmetic — parts that do
    # not sum exactly to the receipt refuse the whole build, nothing writes.
    covering = {y.card for s, y in yes.items() if s in {str(i["sha256"]) for i in items}}
    splits: dict[str, str] = {}
    for card in [legacy] if legacy else [c for c in yes_cards if int(c["id"]) in covering]:
        splits.update(_split_directives(card))
    lines: list[ManifestLine] = []
    for i in items:
        sha = str(i["sha256"])
        base = dict(
            receipt_file=str(i["file"]),
            receipt_sha256=sha,
            vendor=str(i.get("vendor") or ""),
            expense_date=str(i.get("expense_date") or ""),
            project=str(i.get("project") or ""),
        )
        directive = splits.pop(sha[:16], None)
        if directive is None:
            lines.append(
                ManifestLine(
                    amount_cents=int(i["amount_cents"]),
                    category=str(i.get("category") or ""),
                    note=str(i["note"]) if i.get("drive") or i.get("bank") else "",
                    **{k: str(i.get(k) or "") for k in _FX_FIELDS},
                    **base,
                )
            )
            continue
        try:
            parts = _parse_split(directive)
        except ValueError as exc:
            return JobOutput(
                status="ok",
                summary=f"expenses report: split for {i['file']} is invalid; nothing built",
                anomalies=[Anomaly(code="expenses.split_invalid", detail=f"{i['file']}: {exc}")],
            )
        if sum(c for c, _, _ in parts) != int(i["amount_cents"]):
            return JobOutput(
                status="ok",
                summary=f"expenses report: split for {i['file']} does not sum to the "
                "receipt; nothing built",
                anomalies=[
                    Anomaly(
                        code="expenses.split_invalid",
                        detail=f"{i['file']}: parts sum "
                        f"{sum(c for c, _, _ in parts)} cents, receipt is "
                        f"{int(i['amount_cents'])} cents",
                    )
                ],
            )
        for n, (cents, category, note) in enumerate(parts, start=1):
            lines.append(
                ManifestLine(amount_cents=cents, category=category, note=note, part=n, **base)
            )
    if splits:
        unknown = ", ".join(sorted(splits))
        return JobOutput(
            status="ok",
            summary="expenses report: split directive names no receipt in this "
            "report; nothing built",
            anomalies=[
                Anomaly(
                    code="expenses.split_invalid",
                    detail=f"split sha prefix(es) {unknown} match no receipt in the report",
                )
            ],
        )

    report_key = f"expense-report:{ctx.tenant_slug}:{person}:{month}:{content[:16]}"
    manifest = ReportManifest(
        report_key=report_key,
        tenant=ctx.tenant_slug,
        person=person,
        month=month,
        total_cents=total,
        lines=lines,
    )

    slug = f"{_person_slug(person)}_EXP-{month}"
    report_dir = filing / month / "reports" / slug
    prefix = ctx.tenant.expenses.file_prefix
    prefix = f"{prefix}_" if prefix else ""
    xlsx = report_dir / f"{prefix}Expense_Report_{month}.xlsx"
    zip_path = report_dir / f"{prefix}Receipts_{month}.zip"
    manifest_path = report_dir / "manifest.json"
    for target in (xlsx, zip_path, manifest_path):
        ctx.guard.check_write(target)
    report_dir.mkdir(parents=True, exist_ok=True)
    _render_workbook(xlsx, manifest, ctx.tenant.identity.legal_name)
    filed_dir = filing / month / "_filed" / content[:16]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for i in items:
            src = Path(str(i["filed_to"]))
            if src.is_file():
                bundle.write(src, arcname=src.name)
    manifest_path.write_text(manifest.model_dump_json(indent=1))

    # Rows commit BEFORE the receipts move (issue #135): a crash mid-move
    # then leaves committed rows, a complete zip, and visibly loose
    # receipts — never a retry that rebuilds the package around an empty
    # zip because the files moved but nothing recorded their consumption.
    now = _now()
    ctx.ledger.conn.execute(
        "INSERT OR IGNORE INTO expense_report (idempotency_key, tenant, person, month, "
        "total_cents, status, report_path, manifest_path, shadow, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report_key,
            ctx.tenant_slug,
            person,
            month,
            total,
            STATUS_OPEN,
            str(xlsx),
            str(manifest_path),
            0,
            now,
            now,
        ),
    )
    report_id = ctx.ledger.conn.execute(
        "SELECT id FROM expense_report WHERE idempotency_key = ?", (report_key,)
    ).fetchone()[0]
    for line in manifest.lines:
        ctx.ledger.conn.execute(
            "INSERT OR IGNORE INTO expense_line (idempotency_key, report_id, tenant, "
            "receipt_file, receipt_sha256, vendor, expense_date, amount_cents, category, "
            "project, note, said_amount, said_currency, fx_rate, fx_date, fx_source, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{report_key}:line:{line.receipt_sha256}" + (f":{line.part}" if line.part else ""),
                report_id,
                ctx.tenant_slug,
                line.receipt_file,
                line.receipt_sha256,
                line.vendor,
                line.expense_date,
                line.amount_cents,
                line.category,
                line.project,
                line.note,
                *(getattr(line, k) for k in _FX_FIELDS),
                now,
            ),
        )
    ctx.ledger.conn.commit()

    for i in items:
        src = Path(str(i["filed_to"]))
        if src.is_file():
            dest = filed_dir / src.name
            ctx.guard.check_write(dest)
            filed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)

    actions.append(f"built {xlsx.name}: ${total / 100:,.2f}, {len(items)} lines")
    events.append(
        EventSpec(
            key=f"expreport-built:{content[:16]}",
            event_type=BUILT_EVENT,
            payload={
                "report_id": report_id,
                "report_key": report_key,
                "person": person,
                "month": month,
                "total_cents": total,
                "lines": len(manifest.lines),
                "report_path": str(xlsx),
            },
        )
    )
    confirm = _confirm_spec(
        ctx,
        {"id": report_id, "person": person, "month": month, "total_cents": total},
        reason=CONFIRM_REASON,
    )
    return JobOutput(
        status="ok",
        summary=f"expenses report: built {person} {month} (${total / 100:,.2f})",
        actions=actions,
        events=events,
        approvals=[confirm],
    )


def _report_run(ctx: JobContext) -> JobOutput:
    person = str(ctx.params.get("person") or "")
    month = str(ctx.params.get("month") or _tenant_month(ctx))
    # 03-F6: a stranded report is recovered whenever the job runs, in either
    # mode, independent of unconsumed receipts (its rows consumed them).
    recovery = _recover_stranded(ctx)
    items = _unconsumed(ctx)

    if person:
        mine = [i for i in items if i.get("person") == person and str(i.get("month")) == month]
        if not mine:
            return _with_recovery(
                recovery,
                JobOutput(
                    status="ok",
                    summary=f"expenses report: nothing unconsumed for {person} {month}",
                ),
            )
        return _with_recovery(recovery, _report_for(ctx, person, month, mine))

    # Survey mode (decision 4's month-end half): one draft card per person
    # holding unconsumed receipts, so the close never waits on loose paper.
    approvals: list[ApprovalSpec] = []
    by_person: dict[str, list[dict]] = {}
    for i in items:
        if str(i.get("month")) == month:
            by_person.setdefault(str(i.get("person")), []).append(i)
    for who, theirs in sorted(by_person.items()):
        approvals.append(
            ApprovalSpec(
                key=f"expdraft:{who}:{month}:{_report_content_hash(theirs)[:12]}",
                action_type=DRAFT_CARD,
                params={"person": who, "month": month, "count": str(len(theirs))},
                reason="unconsumed receipts for the month; run "
                f"`expenses report --param person={who}` to build the report",
            )
        )
    return _with_recovery(
        recovery,
        JobOutput(
            status="ok",
            summary=f"expenses report survey: {len(approvals)} person(s) with unconsumed "
            f"receipts in {month}",
            approvals=approvals,
        ),
    )


def _categories_by_sha(ctx: JobContext) -> dict[str, str]:
    """The category each receipt was last placed in, by the person's words or a
    reviewer's (``expense.category_set``); later events win."""
    out: dict[str, str] = {}
    for e in _events_of(ctx, CATEGORY_EVENT):
        payload = e.get("payload", {})
        if payload.get("sha256"):
            out[str(payload["sha256"])] = str(payload.get("category", ""))
    return out


def _placed(ctx: JobContext, items: list[dict], extra: dict[str, str] | None = None) -> list[dict]:
    placed = {**_categories_by_sha(ctx), **(extra or {})}
    return [
        {**i, "category": placed[str(i.get("sha256"))]} if str(i.get("sha256")) in placed else i
        for i in items
    ]


def _report_for(
    ctx: JobContext,
    person: str,
    month: str,
    mine: list[dict],
    placed: dict[str, str] | None = None,
) -> JobOutput:
    """One person's month: door claims priced (and any new rate recorded),
    categories placed, owner price directives applied, then the report built
    or its card raised. ``placed``: categories this run is recording. The
    person's ministry drives ride the same card."""
    mine = _with_statements(ctx, month, person, mine)
    mine, rate_events = _price_claims(ctx, _placed(ctx, mine, placed))
    priced, price_events, price_error = _price_items(mine, _price_directives(ctx), person, month)
    if price_error is not None:
        return price_error.model_copy(update={"events": [*rate_events, *price_error.events]})
    recorded, prices = _categories_by_sha(ctx), _owner_prices_by_sha(ctx)
    changed = {s for s, c in (placed or {}).items() if recorded.get(s) != c}
    for e in price_events:
        was = prices.get(str(e.payload["sha256"]), {})
        if any(was.get(k) != e.payload.get(k) for k in ("amount_cents", "category", "note")):
            changed.add(str(e.payload["sha256"]))
    output = _build_report(ctx, person, month, priced, frozenset(changed))
    events = [*rate_events, *price_events, *output.events]
    return output.model_copy(update={"events": events}) if events else output


# ---- job: month -------------------------------------------------------------


def _month_of(ctx: JobContext) -> str:
    """The month this run reviews: the one named, else the month that just
    ended on the tenant's own clock (the review follows the month)."""
    named = str(ctx.params.get("month") or "")
    if named:
        return named
    first = datetime.strptime(_tenant_month(ctx) + "-01", "%Y-%m-%d")
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _month_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expmonth")
    key.param("month")
    for name in sorted(k for k, _v in ctx.params.items() if k.startswith("price:")):
        key.param(name)  # owner price directives, as the report job reads them
    month = _month_of(ctx)
    key.add("month_resolved", month)
    items = [i for i in _unconsumed(ctx) if str(i.get("month")) == month]
    key.add("content", _report_content_hash(items))
    key.add("drives", _report_content_hash(_drive_items(ctx, month)))
    bank = _bank_items(ctx, month)
    if bank:  # a tenant with no statements keeps the key it always had
        key.add("bank", _report_content_hash(bank))
    rates, pending_day = _fx_inputs(ctx, items)
    key.value("fx", rates)
    key.value("placed", sorted(_categories_by_sha(ctx).items()))
    key.add("fx_pending_day", pending_day)
    key.value("approved", sorted(c["idempotency_key"] for c in _approved_cards(ctx, REVIEW_CARD)))
    key.value("stranded", [r["id"] for r in _stranded_reports(ctx)])
    rules = _rules_in_force_key(ctx)
    if rules is not None:  # a ministry with no rules in force keeps the key it had
        key.value("rules", rules)
        key.config("fiscal")
    key.param("filing_dir")
    key.config("expenses", "identity")
    key.config("books.cost_object")  # the fund or project label on a card line
    key.config("institution")
    return key.digest()


def _month_run(ctx: JobContext) -> JobOutput:
    """The missionary's month (alpha step 3): every person holding receipts
    for the month gets one report and one review card, so the reviewer's yes
    is the only step left. Approved cards build their report on the next run.
    A ministry whose institution takes its claims ([institution] submit_to)
    gets no report here: the send job carries those receipts, so none is
    ever both claimed from the institution and paid by the books."""
    month = _month_of(ctx)
    institution = ctx.tenant.institution
    if institution.submit_to:
        return JobOutput(
            status="ok",
            summary=f"expenses month {month}: receipts go to "
            f"{institution.name or 'the institution'} on each person's yes (the send job)",
        )
    recovery = _recover_stranded(ctx)
    by_person: dict[str, list[dict]] = {}
    for i in _unconsumed(ctx):
        if str(i.get("month")) == month:
            by_person.setdefault(str(i.get("person")), []).append(i)
    for d in [*_drive_items(ctx, month), *_bank_items(ctx, month)]:
        by_person.setdefault(d["person"], [])
    summaries, actions, events, approvals, anomalies = [], [], [], [], []
    seen: set[str] = set()
    for person, mine in sorted(by_person.items()):
        out = _report_for(ctx, person, month, mine)
        summaries.append(out.summary)
        actions += out.actions
        approvals += out.approvals
        anomalies += out.anomalies
        for e in out.events:
            if e.key not in seen:
                seen.add(e.key)
                events.append(e)
    return _with_recovery(
        recovery,
        JobOutput(
            status="ok",
            summary=f"expenses month {month}: "
            + ("; ".join(summaries) if summaries else "no receipts"),
            actions=actions,
            events=events,
            approvals=approvals,
            anomalies=anomalies,
        ),
    )


# ---- job: categorize --------------------------------------------------------


def _categorize_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expcat")
    sha = str(key.param("sha"))
    for name in ("category", "by", "said", "filing_dir"):
        key.param(name)
    for name in sorted(k for k, _v in ctx.params.items() if k.startswith("price:")):
        key.param(name)  # a month raised again reads owner price directives too
    # The same words twice is one change; a move back after another move is new.
    key.add(
        "before",
        sum(
            1 for e in _events_of(ctx, CATEGORY_EVENT) if e.get("payload", {}).get("sha256") == sha
        ),
    )
    key.config("expenses", "identity", "books.cost_object")
    return key.digest()


def _categorize_run(ctx: JobContext) -> JobOutput:
    """Place one receipt in a category (the owner's decisions of 2026-10-13):
    by the person's own words when it lands, or by a reviewer, or the person,
    asking their AI. A category moves no money, so the word decides; the
    change is recorded with who said it, and a month already waiting for its
    yes is raised again showing it, before anyone says yes."""
    sha = str(ctx.params.get("sha") or "")
    by = str(ctx.params.get("by") or "")
    said = str(ctx.params.get("said") or "")
    listed = list(ctx.tenant.expenses.category_accounts)
    category = cats.canonical(str(ctx.params.get("category") or ""), listed)
    if category is None:
        names = ", ".join(listed) or "none yet"
        return JobOutput(
            status="ok",
            summary=f"expenses categorize: no category {ctx.params.get('category')!r} "
            f"(the ministry's are {names})",
        )
    receipt = next((i for i in _unconsumed(ctx) if i["sha256"] == sha), None)
    if receipt is None:
        return JobOutput(
            status="ok", summary="expenses categorize: no such receipt waiting in a month"
        )
    before = sum(
        1 for e in _events_of(ctx, CATEGORY_EVENT) if e.get("payload", {}).get("sha256") == sha
    )
    event = EventSpec(
        key=f"expcat:{sha}:{before}",
        event_type=CATEGORY_EVENT,
        payload={"sha256": sha, "category": category, "by": by, "said": said},
    )
    person, month = str(receipt.get("person")), str(receipt.get("month"))
    output = JobOutput(
        status="ok",
        summary=f"expenses categorize: {receipt.get('file')} -> "
        f"{category or cats.UNCATEGORIZED} (by {by or 'nobody named'})",
        events=[event],
    )
    if not _pending_review_keys(ctx, person, month):
        return output
    mine = [i for i in _unconsumed(ctx) if i.get("person") == person and i.get("month") == month]
    again = _report_for(ctx, person, month, mine, placed={sha: category})
    return again.model_copy(
        update={"summary": f"{output.summary}; {again.summary}", "events": [event, *again.events]}
    )


# ---- job: send --------------------------------------------------------------


def _claim_states(ctx: JobContext) -> dict[str, tuple[str, int]]:
    """Each receipt's latest send outcome and how many times it was held."""
    out: dict[str, tuple[str, int]] = {}
    for e in ctx.ledger.read_event_log():
        kind = e.get("event_type")
        if kind not in (SENT_EVENT, HELD_EVENT):
            continue
        sha = str(e.get("payload", {}).get("receipt", ""))
        held = out.get(sha, ("", 0))[1] + (kind == HELD_EVENT)
        out[sha] = (SENT if kind == SENT_EVENT else HELD, held)
    return out


def _words_by_sha(ctx: JobContext) -> dict[str, str]:
    """The person's own words for each receipt, as the door recorded them."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"].get("said", ""))
        for e in _events_of(ctx, CATEGORY_EVENT)
        if e.get("payload", {}).get("sha256") and e["payload"].get("said")
    }


def _send_cards(ctx: JobContext, status: str) -> list[dict]:
    prefix = f"apr:{ctx.tenant_slug}:expenses:{SEND_CARD}:"
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM approval_queue WHERE tenant = ? AND action_type = ? AND status = ?",
        (ctx.tenant_slug, SEND_CARD, status),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "key": str(r["idempotency_key"]).removeprefix(prefix),
            "receipts": {
                s for s in json.loads(r["params_json"] or "{}").get("receipts", "").split(",") if s
            },
        }
        for r in rows
    ]


def _unsent(ctx: JobContext) -> list[dict]:
    states = _claim_states(ctx)
    return [i for i in _unconsumed(ctx) if states.get(i["sha256"], ("", 0))[0] != SENT]


def _send_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expsend")
    key.config("institution", "expenses", "identity")
    items = _unsent(ctx)
    key.add("content", _report_content_hash(items))
    key.value("words", sorted(_words_by_sha(ctx).items()))
    key.value(
        "cards", sorted((c["id"], s) for s in ("pending", "approved") for c in _send_cards(ctx, s))
    )
    rates, pending_day = _fx_inputs(ctx, items)
    key.value("fx", rates)
    key.add("fx_pending_day", pending_day)
    # A held claim is tried again on the next run, never skipped as a re-run.
    states = _claim_states(ctx)
    held = any(states.get(i["sha256"], ("", 0))[0] == HELD for i in items)
    key.add("held_retry", datetime.now(UTC).isoformat() if held else "")
    return key.digest()


def _claim_of(item: dict, words: dict[str, str]):
    from ...contracts.institution import Claim

    name = str(item.get("file", ""))
    cents = item.get("amount_cents")
    usd = f"{Decimal(int(cents)) / 100:.2f}" if cents is not None else ""
    if item.get("said_currency"):
        amount, currency = str(item["said_amount"]), str(item["said_currency"])
    else:
        amount, currency = usd, "USD" if usd else ""
    return Claim(
        receipt=str(item["sha256"]),
        person=str(item.get("person", "")),
        day=name[:10] if _is_door_receipt(name) else "",
        amount=amount,
        currency=currency,
        usd=usd,
        said=words.get(str(item["sha256"]), ""),
        photo=str(item.get("filed_to") or name),
    )


def _claim_line(claim) -> str:
    """One receipt on the person's card, the way they said it."""
    if claim.currency and claim.currency != "USD":
        paid = f"{claim.currency} {Decimal(claim.amount):,.2f}"
    else:
        paid = f"${Decimal(claim.amount):,.2f}" if claim.amount else "amount not known yet"
    return f"{claim.day or 'undated'}: {paid}" + (f", {claim.said}" if claim.said else "")


def _send_run(ctx: JobContext) -> JobOutput:
    """A missionary's receipts to their institution (Robb's idea 1, decided
    2026-10-13). Receipts not yet sent wait on one card per person; their
    yes sends them on the next run, each claim as they said it, priced in
    dollars at its own day's rate when that rate is known. A claim the
    institution doesn't take is held in plain words and sent on a later run
    with no second yes. Every send is to the institution's own door, behind
    the person's card (invariant 7)."""
    institution = ctx.tenant.institution
    if not institution.submit_to:
        return JobOutput(status="ok", summary="expenses send: no institution takes claims here")
    who = institution.name or "the institution"
    unsent = {i["sha256"]: i for i in _unsent(ctx)}
    yes = set().union(*(c["receipts"] for c in _send_cards(ctx, "approved")))
    priced, events = _price_claims(ctx, [unsent[s] for s in sorted(unsent)])
    words = _words_by_sha(ctx)
    claims = {i["sha256"]: _claim_of(i, words) for i in priced}
    actions: list[str] = []

    ready = [claims[s] for s in sorted(yes) if s in claims]
    if ready and ctx.shadow:
        actions.append(f"would send {len(ready)} claim(s) to {who}")
    elif ready:
        sink = claim_sink(institution, store=ctx.ledger.root / "institution")
        states = _claim_states(ctx)
        for out in submit(sink, ready, institution=institution.name):
            if out.status == SENT:
                actions.append(f"sent {out.receipt[:12]} to {who} as {out.reference}")
                events.append(
                    EventSpec(
                        key=f"instsent:{out.receipt}",
                        event_type=SENT_EVENT,
                        payload={"receipt": out.receipt, "reference": out.reference},
                    )
                )
            else:
                n = states.get(out.receipt, ("", 0))[1]
                actions.append(f"held {out.receipt[:12]}: {who} isn't answering")
                events.append(
                    EventSpec(
                        key=f"instheld:{out.receipt}:{n}",
                        event_type=HELD_EVENT,
                        payload={"receipt": out.receipt, "say": out.say},
                    )
                )

    approvals = []
    by_person: dict[str, list] = {}
    for sha, claim in claims.items():
        if sha not in yes:
            by_person.setdefault(claim.person, []).append(claim)
    pending = _send_cards(ctx, "pending")
    for person, mine in sorted(by_person.items()):
        mine.sort(key=lambda c: (c.day, c.receipt))
        shas = {c.receipt for c in mine}
        digest = hashlib.sha256(",".join(sorted(shas)).encode()).hexdigest()[:16]
        n = len(mine)
        approvals.append(
            ApprovalSpec(
                key=f"instsend:{_person_slug(person)}:{digest}",
                action_type=SEND_CARD,
                params={
                    "person": person,
                    "institution": who,
                    "count": str(n),
                    "what": f"{n} receipt{'s' if n != 1 else ''} to send to {who}",
                    "receipts": ",".join(sorted(shas)),
                    "lines": "; ".join(_claim_line(c) for c in mine),
                },
                reason=f"{person}'s receipts not yet sent; approve to send them to {who}",
                supersedes_keys=[c["key"] for c in pending if c["receipts"] < shas],
            )
        )
    return JobOutput(
        status="ok",
        summary=f"expenses send: {len(ready)} to {who} on a yes, "
        f"{sum(len(m) for m in by_person.values())} waiting on a card",
        actions=actions,
        events=events,
        approvals=approvals,
    )


# ---- job: amount ------------------------------------------------------------


def _amount_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expamount")
    sha = str(key.param("sha"))
    for name in ("tag", "said", "by", "read", "payee", "category", "filing_dir"):
        key.param(name)
    key.add(
        "before",
        sum(
            1
            for e in ctx.ledger.read_event_log()
            if e.get("event_type") in (AMOUNT_EVENT, READ_EVENT)
            and e.get("payload", {}).get("sha256") == sha
        ),
    )
    key.config("expenses")
    return key.digest()


def _amount_run(ctx: JobContext) -> JobOutput:
    """A door receipt's amount after its photo (board item t-receipt-photo,
    2026-10-13): what its photo reads (``read=photo``, a proposal) or what
    the person said (their words, or their yes to the reading). The door
    reads the words with code (core/tools/receipts.claim_in) and passes the
    claim as a tag; nothing here prices anything, the month does that from
    the tag the way it prices a claim said with the photo."""
    sha = str(ctx.params.get("sha") or "")
    tag = str(ctx.params.get("tag") or "")
    if not _TAG.match(tag):
        return JobOutput(status="ok", summary=f"expenses amount: {tag!r} is not an amount")
    receipt = next((i for i in _unconsumed(ctx) if i["sha256"] == sha), None)
    if receipt is None or not _is_door_receipt(str(receipt.get("file", ""))):
        return JobOutput(status="ok", summary="expenses amount: no such door receipt waiting")
    read = ctx.params.get("read") == "photo"
    kind = READ_EVENT if read else AMOUNT_EVENT
    before = sum(
        1
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == kind and e.get("payload", {}).get("sha256") == sha
    )
    payload = {"sha256": sha, "tag": tag}
    if read:  # who it reads as paid, and for what: proposals beside the amount
        payload |= {k: str(ctx.params[k]) for k in ("payee", "category") if ctx.params.get(k)}
    else:
        payload |= {
            "by": str(ctx.params.get("by") or ""),
            "said": str(ctx.params.get("said") or ""),
        }
    return JobOutput(
        status="ok",
        summary=f"expenses amount: {receipt.get('file')} "
        + (
            f"reads {tag} (a proposal)"
            if read
            else f"is {tag} (said by {payload['by'] or 'nobody named'})"
        ),
        events=[
            EventSpec(key=f"expamount:{kind}:{sha}:{before}", event_type=kind, payload=payload)
        ],
    )


# ---- job: date --------------------------------------------------------------


def _date_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expdate")
    sha = str(key.param("sha"))
    for name in ("day", "said", "by", "read"):
        key.param(name)
    key.add("before", sum(1 for e in _events_of(ctx, DATED_EVENT) if _payload_sha(e) == sha))
    key.config("expenses")
    return key.digest()


def _payload_sha(e: dict) -> str:
    return str(e.get("payload", {}).get("sha256", ""))


def _date_run(ctx: JobContext) -> JobOutput:
    """A receipt's own day, for one sent later (t-catch-up, 2026-10-14): what
    the person said, or what its photo reads (``read=photo``). It files the
    receipt in that day's month and prices it at that day's rate; nothing
    here prices anything."""
    sha = str(ctx.params.get("sha") or "")
    day = str(ctx.params.get("day") or "")
    try:
        date.fromisoformat(day)
    except ValueError:
        return JobOutput(status="ok", summary=f"expenses date: {day!r} is not a day")
    if len(day) != 10 or not any(i["sha256"] == sha for i in _unconsumed(ctx)):
        return JobOutput(status="ok", summary="expenses date: no such receipt waiting")
    before = sum(1 for e in _events_of(ctx, DATED_EVENT) if _payload_sha(e) == sha)
    payload = {
        "sha256": sha,
        "day": day,
        "said": str(ctx.params.get("said") or ""),
        "by": str(ctx.params.get("by") or ""),
        "read": str(ctx.params.get("read") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses date: {sha[:12]} is from {day}",
        events=[EventSpec(key=f"expdate:{sha}:{before}", event_type=DATED_EVENT, payload=payload)],
    )


# ---- job: drive -------------------------------------------------------------


def _drive_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expdrive")
    for name in ("person", "start", "end", "miles"):
        key.param(name)
    key.config("expenses")
    return key.digest()


def _drive_run(ctx: JobContext) -> JobOutput:
    """One drive the box's tracker ended (core/mileage; Robb's idea 2,
    2026-10-13): the person as the expense tree names them, when it started
    and ended, and its miles. Nothing about where; what it was for comes
    from the person's own words in the weekly check-in."""
    who = str(ctx.params.get("person") or "")
    name = next(
        (
            p.name
            for p in _persons(ctx)
            if re.sub(r"[^a-z0-9]+", "-", p.name.casefold()).strip("-") == who
        ),
        "",
    )
    try:
        miles = Decimal(str(ctx.params.get("miles")))
        start = datetime.fromisoformat(str(ctx.params.get("start")))
        end = datetime.fromisoformat(str(ctx.params.get("end")))
    except (ArithmeticError, ValueError):
        return JobOutput(status="ok", summary="expenses drive: not a drive")
    if not name or miles <= 0 or end < start:
        return JobOutput(status="ok", summary=f"expenses drive: nobody files expenses as {who!r}")
    payload = {
        "person": name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "miles": str(miles),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses drive: {name}, {miles} miles",
        events=[
            EventSpec(
                key=f"drive:{who}:{payload['start']}", event_type=DRIVE_EVENT, payload=payload
            )
        ],
    )


_PERSONAL = ("personal", "private", "not ministry", "not for the ministry", "my own")


def is_personal(said: str) -> bool:
    """Words that say a drive wasn't the ministry's."""
    text = said.casefold()
    return any(word in text for word in _PERSONAL)


def _purpose_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "exppurpose")
    drive = str(key.param("drive"))
    for name in ("said", "by"):
        key.param(name)
    key.add(
        "before",
        sum(1 for e in _events_of(ctx, PURPOSE_EVENT) if e["payload"].get("drive") == drive),
    )
    key.config("expenses")
    return key.digest()


def _purpose_run(ctx: JobContext) -> JobOutput:
    """What one drive was for, as the person said it (t-mileage part 2): a
    purpose, or that it was personal. The latest word wins. Code reads the
    words; nothing here prices anything."""
    drive = str(ctx.params.get("drive") or "")
    said = str(ctx.params.get("said") or "").strip()
    if not said or not any(drive_id(e["payload"]) == drive for e in _events_of(ctx, DRIVE_EVENT)):
        return JobOutput(status="ok", summary="expenses drive purpose: no such drive")
    before = sum(1 for e in _events_of(ctx, PURPOSE_EVENT) if e["payload"].get("drive") == drive)
    payload = {
        "drive": drive,
        "ministry": not is_personal(said),
        "said": said,
        "by": str(ctx.params.get("by") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses drive purpose: {'ministry' if payload['ministry'] else 'personal'}",
        events=[
            EventSpec(key=f"purpose:{drive}:{before}", event_type=PURPOSE_EVENT, payload=payload)
        ],
    )


# ---- job: match -------------------------------------------------------------


def _qbo_write_client(ctx: JobContext):
    """Factory hook: evals monkeypatch this with a fake."""
    from ...adapters.qbo import QboClient

    token_file = ctx.params.get("qbo_token_file") or ctx.tenant.qbo.token_file
    if not token_file:
        raise ValueError("no QBO token file: set [qbo].token_file in tenant.toml")
    return QboClient(token_file)


def _reports(ctx: JobContext, status: str) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM expense_report WHERE tenant = ? AND status = ? ORDER BY id",
        (ctx.tenant_slug, status),
    ).fetchall()
    return [dict(r) for r in rows]


def _confirmed_report_ids(ctx: JobContext) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for card in _approved_cards(ctx, CONFIRM_CARD):
        rid = card["params"].get("report_id", "")
        if str(rid).isdigit():
            out[int(rid)] = card
    return out


# Bumped when match semantics change, so a code fix re-runs against the same
# inputs instead of replaying the prior result — the PR #62 / issue #115
# gap-3 lesson applied to code changes, not just config changes. v2:
# issue #133 (leg-3 window anchors to the report month; line consumption).
MATCH_VERSION = "2"


def _match_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expmatch", version=MATCH_VERSION)
    key.value("open", [f"{r['id']}:{r['status']}" for r in _reports(ctx, STATUS_OPEN)])
    key.value("recorded", [f"{r['id']}:{r['status']}" for r in _reports(ctx, STATUS_RECORDED)])
    unverified = [f"{r['id']}:{r['qbo_purchase_id']}" for r in _unverified_reports(ctx)]
    key.value("unverified", unverified)
    if unverified:
        key.value("verify_day", _verify_day(ctx))
    key.value("confirmed", sorted(str(i) for i in _confirmed_report_ids(ctx)))
    # 03-F7: the newest confirm card's state per open report is a run input.
    # A reject with nothing else changing must re-fire, or the re-ask (a
    # fresh key naming the rejected card) can never be parked.
    key.value(
        "confirm_cards",
        [
            f"{r['id']}:{state}:{card_id}"
            for r in _reports(ctx, STATUS_OPEN)
            for state, card_id in (_confirm_card_state(ctx, int(r["id"])),)
        ],
    )
    # Issue #115 gap 3: the category mapping is a run input — a config fix
    # must produce a fresh key, or a parked report replays parked forever.
    # The same holds for the vendor-role inputs (issue #120). The #153 audit
    # added the rest of the section (window, default channel) and the bank
    # CSV format: the whole section, uniformly.
    key.config("expenses", "bank_csv", "qbo", "identity.timezone", "close.bank_account")
    csv = str(key.param("bank_csv") or "")
    if csv and Path(csv).is_file():
        key.file(csv, label="bank_csv")
    return key.digest()


def _lines_for(ctx: JobContext, report_id: int) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM expense_line WHERE report_id = ? ORDER BY id", (report_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _purchase_payload(
    report: dict,
    lines: list[dict],
    *,
    payee_id: str,
    bank_account_id: str,
    account_ids: dict[str, str],
    txn_date: str,
    group_by: str = "category",
) -> dict:
    # Owner/employee reports split one line per category; a vendor-role
    # report splits one line per project (issue #120) — same machinery,
    # different grouping key, account_ids keyed to match.
    by_key: dict[str, int] = {}
    for line in lines:
        by_key[line[group_by]] = by_key.get(line[group_by], 0) + line["amount_cents"]
    return {
        "PaymentType": "Check",
        "AccountRef": {"value": bank_account_id},
        "EntityRef": {"value": payee_id, "type": "Vendor"},
        "TxnDate": txn_date or None,
        "DocNumber": f"EXP-{report['id']}"[:21],
        "PrivateNote": f"engine:{report['idempotency_key']}",
        "Line": [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": round(cents / 100, 2),
                "Description": f"{report['person']} expense report {report['month']} — {key}",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_ids[key]}},
            }
            for key, cents in sorted(by_key.items())
        ],
    }


_REPORT_FIELDS = frozenset(
    {"qbo_purchase_id", "reimbursed_channel", "instrument_ref", "reimbursed_date", "cleared_date"}
)


def _sets(fields: dict) -> str:
    """``col = ?`` for each field. A column name is interpolated into the SQL,
    so only expense_report's settable columns may arrive; values stay ? marks."""
    unknown = set(fields) - _REPORT_FIELDS
    if unknown:
        raise ValueError(f"expense_report has no settable {sorted(unknown)}")
    return ", ".join(f"{k} = ?" for k in fields)


def _set_report_status(ctx: JobContext, report_id: int, status: str, **fields) -> None:
    sets = _sets(fields)
    sql = f"UPDATE expense_report SET status = ?, updated_at = ?{', ' + sets if sets else ''} "  # noqa: S608  (columns from _REPORT_FIELDS only)
    sql += "WHERE id = ?"
    ctx.ledger.conn.execute(sql, (status, _now(), *fields.values(), report_id))
    ctx.ledger.conn.commit()


def _update_report(ctx: JobContext, report_id: int, **fields) -> None:
    """Field update that leaves the status alone."""
    sets = _sets(fields)
    ctx.ledger.conn.execute(
        f"UPDATE expense_report SET updated_at = ?, {sets} WHERE id = ?",  # noqa: S608  (columns from _REPORT_FIELDS only)
        (_now(), *fields.values(), report_id),
    )
    ctx.ledger.conn.commit()


def _unverified_reports(ctx: JobContext) -> list[dict]:
    """Open reports that already carry a Purchase id: written, not yet read
    back right (honesty audit 2026-09-03, 03-F1)."""
    return [r for r in _reports(ctx, STATUS_OPEN) if r["qbo_purchase_id"]]


def _verify_day(ctx: JobContext) -> str:
    """The tenant-local date. A match-key input only while an unverified
    Purchase exists, so the verify pass retries once a day instead of
    replaying a transport failure forever."""
    return clock.local_date(_now(), ctx.tenant.identity.timezone)


def _own_purchase_ids(ctx: JobContext) -> set[str]:
    """Every Purchase id the engine wrote, verified or not (W1 rule 3: never
    read our own writing as evidence, and never mistake it for the owner's
    hand-coding)."""
    rows = ctx.ledger.conn.execute(
        "SELECT qbo_purchase_id FROM expense_report WHERE tenant = ? "
        "AND qbo_purchase_id IS NOT NULL AND qbo_purchase_id != ''",
        (ctx.tenant_slug,),
    ).fetchall()
    return {str(r["qbo_purchase_id"]) for r in rows}


def _readback_purchase(client, purchase_id: str, total_cents: int) -> tuple[bool, str]:
    """Read a Purchase back and compare the total. Returns (verified, note).
    Never raises: the Purchase exists whatever the readback does, and the
    caller has already remembered its id."""
    expected = round(total_cents / 100, 2)
    try:
        readback = client.get_purchase(purchase_id)
    except Exception as exc:  # any transport failure, not only the API's own errors
        return False, f"readback failed: {exc}"
    got = readback.get("TotalAmt")
    try:
        if round(float(got), 2) == expected:
            return True, "readback ok"
    except (TypeError, ValueError):
        pass
    return False, f"readback {got!r}, expected {expected}"


def _complete_record(ctx: JobContext, report: dict, card: dict, purchase_id: str) -> None:
    """The verified write becomes the record: status flips to Recorded with
    the card's provenance (channel, instrument, tenant-local booked date)."""
    tz = ctx.tenant.identity.timezone
    _set_report_status(
        ctx,
        report["id"],
        STATUS_RECORDED,
        qbo_purchase_id=purchase_id,
        reimbursed_channel=str(card.get("params", {}).get("channel", "")),
        instrument_ref=str(card.get("params", {}).get("instrument_ref", "")),
        reimbursed_date=clock.local_date(str(card.get("resolved_at") or ""), tz),
    )


def _readback_anomaly(report_id: int, purchase_id: str, note: str) -> Anomaly:
    code = (
        "expenses.qbo_readback_failed"
        if note.startswith("readback failed")
        else "expenses.qbo_readback_mismatch"
    )
    return Anomaly(
        code=code,
        detail=f"purchase {purchase_id} for report #{report_id}: {note}; id kept on the "
        "report (Open, unverified), retried daily; stopped",
    )


def _match_run(ctx: JobContext) -> JobOutput:  # noqa: C901  # complexity 41, tracked debt
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []

    open_reports = _reports(ctx, STATUS_OPEN)
    confirmed = _confirmed_report_ids(ctx)
    window = timedelta(days=ctx.tenant.expenses.match_window_days)

    # -- leg 1: record confirmed reimbursements in QBO ------------------------
    to_record = [r for r in open_reports if r["id"] in confirmed and not r["qbo_purchase_id"]]
    if to_record and ctx.shadow:
        for r in to_record:
            actions.append(f"would record report #{r['id']} (${r['total_cents'] / 100:,.2f})")
        to_record = []
    # parked = a card the owner can clear; blocked = an anomaly that parked
    # nothing (03-F12): the two used to share one counter, so "parked 1"
    # stood over an empty approvals list.
    recorded = parked = blocked = unverified = 0
    client = None

    # -- leg 1a: verify Purchases written but not yet read back right ---------
    # (honesty audit 2026-09-03, 03-F1). The id was remembered at create time;
    # a good readback now completes the record, a bad one flags again.
    to_verify = [r for r in open_reports if r["qbo_purchase_id"]] if not ctx.shadow else []
    for report in to_verify:
        client = client or _qbo_write_client(ctx)
        purchase_id = str(report["qbo_purchase_id"])
        ok, note = _readback_purchase(client, purchase_id, report["total_cents"])
        if not ok:
            unverified += 1
            anomalies.append(_readback_anomaly(report["id"], purchase_id, note))
            continue
        _complete_record(ctx, report, confirmed.get(report["id"], {}), purchase_id)
        recorded += 1
        actions.append(f"verified report #{report['id']} -> Purchase {purchase_id}")
        events.append(
            EventSpec(
                key=f"exppurchase-verified:{report['idempotency_key']}",
                event_type="expense.qbo_purchase_verified",
                payload={"report_id": report["id"], "qbo_purchase_id": purchase_id},
            )
        )

    if to_record:
        from ...adapters.qbo import QboApiError

        client = client or _qbo_write_client(ctx)
        vendor_map = {
            str(v["display_name"]).strip().lower(): v["id"] for v in client.fetch_vendors()
        }
        account_map = {a["fully_qualified_name"]: a["id"] for a in client.fetch_accounts()}
        tz = ctx.tenant.identity.timezone
        since = min(
            clock.local_date(str(confirmed[r["id"]].get("resolved_at") or _now()), tz)
            for r in to_record
        )
        recent = client.fetch_recent_txns(since=since)
        own_ids = _own_purchase_ids(ctx)
        for report in to_record:
            card = confirmed[report["id"]]
            # The approval instant is stored UTC; the booked date is a
            # tenant-calendar fact. An evening approval must not post the
            # reimbursement to UTC's next day (#136).
            txn_date = clock.local_date(str(card.get("resolved_at") or ""), tz)
            payee_name = _payee_name(ctx, report["person"])
            payee_id = vendor_map.get(payee_name.strip().lower())
            if payee_id is None:
                parked += 1
                approvals.append(
                    ApprovalSpec(
                        key=f"expmap:payee:{report['id']}",
                        action_type="expenses.qbo_map_payee",
                        params={
                            "report_id": str(report["id"]),
                            "person": report["person"],
                            "payee": payee_name,
                        },
                        reason="no accounting-system payee matches this person; create "
                        "or map the vendor record",
                    )
                )
                continue
            bank_name = ctx.tenant.close.bank_account
            bank_id = account_map.get(bank_name)
            if bank_id is None:
                blocked += 1
                anomalies.append(
                    Anomaly(
                        code="expenses.qbo_bank_unmapped",
                        detail=f"[close].bank_account {bank_name!r} not in the chart",
                    )
                )
                continue
            lines = _lines_for(ctx, report["id"])
            person_cfg = _person_named(ctx, report["person"])
            role = getattr(person_cfg, "role", "owner") if person_cfg else "owner"
            if role == "vendor":
                # Vendor-role booking (issue #120): every line is a project
                # cost on the project's own chart account — no overhead
                # accounts, the per-category table is never consulted.
                projects = sorted({str(line["project"] or "") for line in lines})
                account_ids, missing = _project_accounts(
                    ctx.tenant.expenses.project_account_template, projects, account_map
                )
                if missing:
                    parked += 1
                    approvals.append(
                        ApprovalSpec(
                            key=f"expmap:proj:{report['id']}",
                            action_type="expenses.qbo_map_project_account",
                            params={
                                "report_id": str(report["id"]),
                                "projects": ", ".join(missing),
                                "template": ctx.tenant.expenses.project_account_template
                                or "(unset)",
                            },
                            reason="no chart account for these projects; create the "
                            "account(s) the template names, or set "
                            "[expenses].project_account_template — never a guess",
                        )
                    )
                    continue
                group_by = "project"
            else:
                group_by = "category"
                categories = sorted({line["category"] for line in lines})
                mapping = ctx.tenant.expenses.category_accounts
                account_ids = {}
                missing = []
                for category in categories:
                    fqn = mapping.get(category, "")
                    acct = account_map.get(fqn) if fqn else None
                    if acct is None:
                        missing.append(category)
                    else:
                        account_ids[category] = acct
                if missing:
                    parked += 1
                    approvals.append(
                        ApprovalSpec(
                            key=f"expmap:acct:{report['id']}",
                            action_type="expenses.qbo_map_category",
                            params={
                                "report_id": str(report["id"]),
                                "categories": ", ".join(missing),
                            },
                            reason="no chart account mapped for these categories; set "
                            "[expenses].category_accounts — never a guess",
                        )
                    )
                    continue
            # W1 rule 2: an existing same-payee same-amount transaction in the
            # window is exactly the owner having hand-coded it already.
            lookalike = None
            for txn in recent:
                if str(txn.get("qbo_id")) in own_ids:
                    continue  # rule 3: never read our own writing as evidence
                if str(txn.get("vendor", "")).strip().lower() != payee_name.strip().lower():
                    continue
                if int(txn.get("amount_cents", -1)) != int(report["total_cents"]):
                    continue
                lookalike = txn
                break
            if lookalike is not None:
                # Record-only (03-F7): nothing consumes this card's resolution,
                # so approving or rejecting it executes nothing and the report
                # stays Open; the stable key is right for a card that only
                # records. It blocks the record rather than parking an ask.
                blocked += 1
                existing = str(lookalike.get("qbo_id"))
                approvals.append(
                    ApprovalSpec(
                        key=f"expdupq:{report['id']}",
                        action_type="expenses.qbo_duplicate_review",
                        params={"report_id": str(report["id"]), "existing": existing},
                        reason=f"a similar record already exists in the accounting system "
                        f"({existing}: same payee, same amount); the engine never writes "
                        "over it. This card is record-only: approving or rejecting it "
                        "executes nothing and the report stays Open until a human "
                        "reconciles the existing record by hand",
                    )
                )
                continue
            payload = _purchase_payload(
                report,
                lines,
                payee_id=payee_id,
                bank_account_id=bank_id,
                account_ids=account_ids,
                txn_date=txn_date,
                group_by=group_by,
            )
            try:
                created = client.create_purchase(payload)
            except QboApiError as exc:
                blocked += 1
                anomalies.append(
                    Anomaly(
                        code="expenses.qbo_purchase_rejected",
                        detail=f"report #{report['id']}: {exc}",
                    )
                )
                continue
            # #314: .get(key, default) falls back only when the key is
            # ABSENT; a QBO create response can carry an explicit null Id,
            # which str(None) turns into the truthy string "None" and would
            # defeat the `if not purchase_id` guard just below.
            purchase_id = str(created.get("Id") or "")
            if not purchase_id:
                anomalies.append(
                    Anomaly(
                        code="expenses.qbo_create_unconfirmed",
                        detail=f"report #{report['id']}: create returned no Id; stopped",
                    )
                )
                break
            # Honesty audit 2026-09-03 (03-F1, S1): the Purchase exists the
            # instant create returns. Remember it BEFORE the readback (status
            # stays Open: unverified) so a readback that raises or disagrees
            # can never leave a Purchase the ledger has forgotten, would
            # re-create, or would mistake for the owner's hand-coding.
            _update_report(ctx, report["id"], qbo_purchase_id=purchase_id)
            verified, note = _readback_purchase(client, purchase_id, report["total_cents"])
            events.append(
                EventSpec(
                    key=f"exppurchase:{report['idempotency_key']}",
                    event_type="expense.qbo_purchase_created",
                    payload={
                        "report_id": report["id"],
                        "person": report["person"],
                        "total_cents": report["total_cents"],
                        "qbo_purchase_id": purchase_id,
                        "verified": verified,
                        "readback": note,
                    },
                )
            )
            if not verified:
                unverified += 1
                anomalies.append(_readback_anomaly(report["id"], purchase_id, note))
                actions.append(
                    f"recorded report #{report['id']} -> Purchase {purchase_id} "
                    f"(UNVERIFIED: {note})"
                )
                break
            _complete_record(ctx, report, card, purchase_id)
            recorded += 1
            actions.append(f"recorded report #{report['id']} -> Purchase {purchase_id}")

    # -- leg 2: safety net — evidence matching an unconfirmed Open report -----
    unconfirmed = [r for r in open_reports if r["id"] not in confirmed]
    if unconfirmed and not ctx.shadow:
        try:
            client = client or _qbo_write_client(ctx)
            evidence = client.fetch_evidence(since=(datetime.now(UTC) - window).date().isoformat())
        except Exception as exc:
            # The net is best-effort; recording never depends on it. But a
            # net that is down must say so (03-F13), or a week of failures
            # reads exactly like a week with no evidence.
            evidence = []
            anomalies.append(
                Anomaly(
                    code="expenses.safety_net_unavailable",
                    detail=f"clearing evidence could not be fetched this run ({exc}); "
                    "no open report was scanned for a matching payment",
                )
            )
        own_ids = _own_purchase_ids(ctx)
        # A vendor-role person's payment clears under the VENDOR record's
        # name, so evidence matches through both names back to the person.
        payee_to_person = {}
        for p in _persons(ctx):
            payee_to_person[p.name.strip().lower()] = p.name
            qbo_vendor = getattr(p, "qbo_vendor", "")
            if qbo_vendor:
                payee_to_person[qbo_vendor.strip().lower()] = p.name
        for ev in evidence:
            payee = str(getattr(ev, "payee", "") or "").strip().lower()
            person_name = payee_to_person.get(payee)
            if person_name is None or str(getattr(ev, "qbo_id", "")) in own_ids:
                continue
            cents = int(getattr(ev, "amount_cents", 0))
            hits = [
                r
                for r in unconfirmed
                if int(r["total_cents"]) == abs(cents) and r["person"] == person_name
            ]
            if len(hits) > 1:
                approvals.append(
                    ApprovalSpec(
                        key=f"expamb:{abs(cents)}:{payee}",
                        action_type="expenses.match_ambiguous",
                        params={
                            "amount_cents": str(abs(cents)),
                            "person": person_name,
                            "report_ids": ",".join(str(r["id"]) for r in hits),
                        },
                        reason="one payment matches two open reports; a human picks, "
                        "never an auto-match",
                    )
                )
            elif len(hits) == 1:
                approvals.append(
                    _confirm_spec(
                        ctx,
                        hits[0],
                        reason="cleared-payment evidence matches this open report; "
                        "approve to let the engine record the split",
                        evidence=str(getattr(ev, "qbo_id", "")),
                    )
                )

    # -- leg 3: CSV clearing (W2 Option B) ------------------------------------
    cleared = 0
    csv_param = str(ctx.params.get("bank_csv") or "")
    if csv_param and not ctx.shadow:
        from ...adapters.bank_csv import parse_bank_csv

        lines = parse_bank_csv(csv_param, ctx.tenant.bank_csv)
        consumed: set[int] = set()
        # Lines consumed by PRIOR runs stay consumed: each past clearing
        # event recorded its line's (date, amount), so re-presenting a CSV
        # containing that line must not let it clear a second same-amount
        # report (issue #133).
        for ev in _events_of(ctx, "expense.reimbursement_cleared"):
            payload = ev.get("payload", {})
            date = str(payload.get("cleared_date") or "")
            cents = int(payload.get("amount_cents") or 0)
            for i, line in enumerate(lines):
                if i in consumed:
                    continue
                if line.date == date and int(abs(line.amount) * 100) == cents:
                    consumed.add(i)
                    break
        for report in _reports(ctx, STATUS_RECORDED):
            total = int(report["total_cents"])
            # Issue #133: payment PRECEDES approval by design (the confirm
            # card's reason says "approve once the reimbursement has been
            # paid" — live shape: check 3050 cleared 8/11, card approved
            # 8/12), so reimbursed_date — the approval day — must never
            # bound the window. The report month is the true floor: a
            # reimbursement cannot clear before the month it reimburses.
            month = str(report["month"] or "")
            start = f"{month}-01" if month else ""
            # The instrument recorded at approval (issue #115: check 3050)
            # disqualifies same-amount lines carrying a DIFFERENT number;
            # a line with no number still clears on amount+date (ACH/Zelle
            # exports omit the check column).
            ref_digits = "".join(re.findall(r"\d+", str(report.get("instrument_ref") or "")))
            for i, line in enumerate(lines):
                if i in consumed:
                    continue
                line_cents = int(abs(line.amount) * 100)
                if line_cents != total:
                    continue
                if start and line.date < start:
                    continue
                line_digits = "".join(re.findall(r"\d+", str(line.check_ref or "")))
                if ref_digits and line_digits and ref_digits != line_digits:
                    continue
                # A line clears exactly one report (issue #133): without
                # consumption, one bank line "explains" every same-amount
                # recorded report.
                consumed.add(i)
                _set_report_status(ctx, report["id"], STATUS_REIMBURSED, cleared_date=line.date)
                cleared += 1
                actions.append(f"report #{report['id']} cleared {line.date}")
                events.append(
                    EventSpec(
                        key=f"expcleared:{report['idempotency_key']}",
                        event_type="expense.reimbursement_cleared",
                        payload={
                            "report_id": report["id"],
                            "cleared_date": line.date,
                            "amount_cents": total,
                        },
                    )
                )
                break

    return JobOutput(
        status="ok",
        summary="expenses match: "
        + (f"unverified {unverified}, " if unverified else "")
        + f"recorded {recorded}, parked {parked}, blocked {blocked}, cleared {cleared}",
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


# ---- job: inbox (issue #112) ------------------------------------------------


def _inbox_dir(ctx: JobContext) -> Path | None:
    configured = ctx.params.get("inbox_dir") or ctx.tenant.expenses.inbox_dir
    return Path(configured).expanduser() if configured else None


def _inbox_person(ctx: JobContext) -> str:
    return str(ctx.params.get("inbox_person") or ctx.tenant.expenses.inbox_person or "")


def _inbox_candidates(ctx: JobContext) -> list[Path]:
    """Images at the inbox ROOT only: ``_not-receipts/`` and any other
    subfolder are out of scope (skips live there; never deleted)."""
    inbox = _inbox_dir(ctx)
    if inbox is None or not inbox.is_dir():
        return []
    return sorted(
        p
        for p in inbox.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and not p.name.endswith(inbox_boundary.LABEL_SIDECAR_SUFFIX)
        and p.suffix.lower() in RECEIPT_SUFFIXES
    )


def _inbox_events_by_sha(ctx: JobContext, event_type: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in _events_of(ctx, event_type):
        payload = e.get("payload", {})
        sha = str(payload.get("sha256", ""))
        if sha:
            out[sha] = payload
    return out


def _inbox_records_by_sha(ctx: JobContext, record_type: str) -> dict[str, dict]:
    """Job records (03-F9) by content hash: the memory of a move a dying run
    made after recording it but before its event landed."""
    out: dict[str, dict] = {}
    for rec in ctx.records(record_type):
        sha = str(rec["payload"].get("sha256", ""))
        if sha:
            out[sha] = rec["payload"]
    return out


def _classifier_kind(ctx: JobContext) -> str:
    return str(ctx.params.get("classifier") or "claude")


def _inbox_key(ctx: JobContext) -> str:
    """Inbox content, config, classifier, and card RESOLUTIONS are all run
    inputs: an approval must re-fire execution instead of replaying the
    propose-only result (the issue #119/#121 replay lesson)."""
    key = RunKey(ctx, "expinbox")
    key.add("inbox_dir", str(_inbox_dir(ctx) or ""))
    key.add("person", _inbox_person(ctx))
    key.add("classifier", str(key.param("classifier") or "claude"))
    key.param("drop_dir")
    key.config("expenses")
    # Row 7.11: classification runs through the gateway, so the resolved
    # inbox_classify tier rides the key (docs/run-keys.md).
    key.config("llm")
    key.value("llm_tier", inbox_boundary.resolved_tier(ctx, _classifier_kind(ctx)))
    key.value("candidates", sorted(f"{p.name}:{_sha256(p)[:16]}" for p in _inbox_candidates(ctx)))
    cards = [
        f"card:{c['id']}:{c['params'].get('project', '')}:{c['params'].get('person', '')}"
        for action in (INBOX_FILE_CARD, INBOX_SKIP_CARD)
        for c in _approved_cards(ctx, action)
    ]
    key.value("cards", cards)
    return key.digest()


def _inbox_run(ctx: JobContext) -> JobOutput:  # noqa: C901  # complexity 25, tracked debt
    inbox = _inbox_dir(ctx)
    if inbox is None:
        return JobOutput(status="ok", summary="expenses inbox: no inbox_dir configured")
    if not inbox.is_dir():
        return JobOutput(status="ok", summary=f"expenses inbox: {inbox} does not exist")
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    drop = _drop_dir(ctx)

    candidates = _inbox_candidates(ctx)
    by_sha = {_sha256(p): p for p in candidates}
    filed_done = _inbox_events_by_sha(ctx, INBOX_FILED_EVENT)
    skipped_done = _inbox_events_by_sha(ctx, INBOX_SKIPPED_EVENT)
    filed_records = _inbox_records_by_sha(ctx, INBOX_FILED_RECORD)
    skip_records = _inbox_records_by_sha(ctx, INBOX_SKIPPED_RECORD)

    # -- execute approved decisions first (this run's files may then flow) --
    # Record-then-move (03-F9): the job record is durable BEFORE the move,
    # so a run that dies between the two leaves a record the next run turns
    # into the event (an action, no anomaly). A moved file with NEITHER
    # record nor event (a hand move, or a run from before records existed)
    # is found where the card said it would go, hash-verified, and recorded
    # with an anomaly instead of being read as "already handled".
    moved = recovered = 0
    for card in _approved_cards(ctx, INBOX_FILE_CARD):
        sha = str(card["params"].get("sha256", ""))
        if sha in filed_done:
            continue
        path = by_sha.get(sha)
        if drop is None:
            if path is not None:
                anomalies.append(
                    Anomaly(
                        code="expenses.inbox_no_drop_dir",
                        detail="approved inbox receipt but [expenses].drop_dir is unset",
                    )
                )
            continue
        person = str(card["params"].get("person") or _inbox_person(ctx))
        project = str(card["params"].get("project", "")).strip()
        # No project on the card -> the person's ROOT: the existing intake
        # attribution card (card-never-guess) takes over from there.
        dest_dir = drop / person / project if project else drop / person
        name = path.name if path is not None else str(card["params"].get("file", ""))
        dest = dest_dir / name
        if path is None:
            # Gone from the inbox with no filing on record: a run recorded the
            # move and died before the event (03-F9, healed from the record),
            # or the human moved it by hand / a pre-record run died (found by
            # hash, recorded with an anomaly).
            if name and dest.is_file() and _sha256_or_none(dest) == sha:
                recovered += 1
                recorded = filed_records.get(sha)
                if recorded is not None:
                    actions.append(
                        f"recorded the filing of {name} from its job record "
                        "(a run died between the move and the record)"
                    )
                else:
                    anomalies.append(
                        Anomaly(
                            code="expenses.inbox_filing_unrecorded",
                            detail=f"{name}: found at {dest_dir} with no expense.inbox_filed "
                            "record (a run failed between the move and the record); "
                            "recorded now",
                        )
                    )
                events.append(
                    EventSpec(
                        key=f"inboxfiled:{sha[:16]}",
                        event_type=INBOX_FILED_EVENT,
                        payload={"sha256": sha, "file": name, "dest": str(dest)},
                    )
                )
            continue
        if ctx.shadow:
            actions.append(f"would file {path.name} -> {dest_dir}")
            continue
        ctx.guard.check_write(dest)
        dest_dir.mkdir(parents=True, exist_ok=True)
        payload = {"sha256": sha, "file": path.name, "dest": str(dest)}
        ctx.record_now(f"inboxfiled:{sha[:16]}", INBOX_FILED_RECORD, payload)  # 03-F9
        shutil.move(str(path), dest)
        moved += 1
        actions.append(f"filed {path.name} -> {dest_dir}")
        events.append(
            EventSpec(key=f"inboxfiled:{sha[:16]}", event_type=INBOX_FILED_EVENT, payload=payload)
        )
    skipped = 0
    not_receipts = inbox / "_not-receipts"
    parked_by_sha: dict[str, Path] | None = None  # _not-receipts/, hashed on demand
    for card in _approved_cards(ctx, INBOX_SKIP_CARD):
        for sha in str(card["params"].get("sha256s", "")).split(","):
            sha = sha.strip()
            if not sha or sha in skipped_done:
                continue
            path = by_sha.get(sha)
            if path is None:
                # Same shape as the filer: a skip moved and never recorded.
                if parked_by_sha is None:
                    parked_by_sha = (
                        {_sha256(p): p for p in sorted(not_receipts.iterdir()) if p.is_file()}
                        if not_receipts.is_dir()
                        else {}
                    )
                parked = parked_by_sha.get(sha)
                if parked is not None:
                    recovered += 1
                    if sha in skip_records:
                        actions.append(
                            f"recorded the skip of {parked.name} from its job record "
                            "(a run died between the move and the record)"
                        )
                    else:
                        anomalies.append(
                            Anomaly(
                                code="expenses.inbox_skip_unrecorded",
                                detail=f"{parked.name}: found in _not-receipts/ with no "
                                "expense.inbox_skipped record (a run failed between the "
                                "move and the record); recorded now",
                            )
                        )
                    events.append(
                        EventSpec(
                            key=f"inboxskipped:{sha[:16]}",
                            event_type=INBOX_SKIPPED_EVENT,
                            payload={"sha256": sha, "file": parked.name},
                        )
                    )
                continue
            dest = not_receipts / path.name
            if ctx.shadow:
                actions.append(f"would move {path.name} -> _not-receipts/")
                continue
            ctx.guard.check_write(dest)
            not_receipts.mkdir(parents=True, exist_ok=True)
            payload = {"sha256": sha, "file": path.name}
            ctx.record_now(f"inboxskipped:{sha[:16]}", INBOX_SKIPPED_RECORD, payload)  # 03-F9
            shutil.move(str(path), dest)
            skipped += 1
            actions.append(f"moved {path.name} -> _not-receipts/ (never deleted)")
            events.append(
                EventSpec(
                    key=f"inboxskipped:{sha[:16]}", event_type=INBOX_SKIPPED_EVENT, payload=payload
                )
            )

    # -- classify what remains unlabelled (event memory, one LLM look ever) --
    classified = _inbox_events_by_sha(ctx, INBOX_CLASSIFIED_EVENT)
    classifier = inbox_boundary.build_classifier(_classifier_kind(ctx), ctx)
    new_receipts = 0
    new_skips: list[tuple[str, str]] = []  # (sha, name)
    for path in _inbox_candidates(ctx):
        sha = _sha256(path)
        if sha in filed_done or sha in skipped_done:
            continue
        prior = classified.get(sha)
        if prior is None:
            label = inbox_boundary.sanitize(classifier.classify(path))
            payload = {
                "sha256": sha,
                "file": path.name,
                "receipt": label.receipt,
                "confidence": label.confidence,
            }
            if label.receipt:
                payload.update(
                    vendor=label.vendor, amount=label.amount, expense_date=label.expense_date
                )
            events.append(
                EventSpec(
                    key=f"inboxcls:{sha[:16]}",
                    event_type=INBOX_CLASSIFIED_EVENT,
                    payload=payload,
                )
            )
            prior = payload
        if prior.get("receipt"):
            new_receipts += 1
            approvals.append(
                ApprovalSpec(
                    key=f"inboxfile:{sha[:16]}",
                    action_type=INBOX_FILE_CARD,
                    params={
                        "file": path.name,
                        "sha256": sha,
                        "person": _inbox_person(ctx),
                        "vendor": str(prior.get("vendor", "")),
                        "amount": str(prior.get("amount", "")),
                        "expense_date": str(prior.get("expense_date", "")),
                        "confidence": str(prior.get("confidence", "")),
                    },
                    reason="inbox image classified as a receipt; approve with "
                    "--param project=P.. (and optionally person=..) to file it "
                    "into the drop tree — without a project it lands at the "
                    "person root and normal attribution takes over",
                )
            )
        else:
            new_skips.append((sha, path.name))

    superseded: list[dict] = []
    if new_skips:
        digest = hashlib.sha256(",".join(sorted(s for s, _ in new_skips)).encode()).hexdigest()
        # Each unanswered night's set contains the last one, so the older card
        # retires instead of waiting to be answered by hand (proposal 99e8ae57).
        superseded = skip_card_supersessions(_pending_skip_cards(ctx), [s for s, _ in new_skips])
        approvals.append(
            ApprovalSpec(
                key=f"inboxskip:{digest[:16]}",
                action_type=INBOX_SKIP_CARD,
                params={
                    # Label-only contract: filenames and count, no content.
                    "count": str(len(new_skips)),
                    "files": "; ".join(sorted(n for _, n in new_skips)),
                    "sha256s": ",".join(sorted(s for s, _ in new_skips)),
                },
                reason="classified as not receipts; approve to move them to "
                "_not-receipts/ (kept, never deleted)",
                supersedes_keys=[entry["key"] for entry in superseded],
            )
        )

    return JobOutput(
        status="ok",
        summary=(
            f"expenses inbox: {new_receipts} receipt proposal(s), "
            f"{len(new_skips)} skip proposal(s), filed {moved}, moved {skipped} to "
            "_not-receipts"
        )
        + (f", new skip card covers {len(superseded)} older pending one(s)" if superseded else "")
        + (
            f", recorded {recovered} move(s) a dying run left without an event" if recovered else ""
        ),
        events=events,
        approvals=approvals,
        actions=actions,
        anomalies=anomalies,
    )


# ---- jobs: statement, school, line purpose (t-statements) -----------------


def _filer(ctx: JobContext, name: str) -> str:
    """The person as the expense tree names them, or ""."""
    return next((p.name for p in _persons(ctx) if p.name == name), "")


def _statement_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expstatement")
    for name in ("person", "account", "lines", "transfers", "by", "said"):
        key.param(name)
    key.config("expenses")
    return key.digest()


def _statement_run(ctx: JobContext) -> JobOutput:
    """Statement lines kept (t-statements, 2026-10-14): every line of the
    ministry's account, or the lines the person picked from their own; and
    the ministry's lines that landed in the person's own account (paid
    back, never spending). The door parsed the file and keeps no copy; a
    line's fingerprint is its event key, so a statement sent twice is the
    same lines."""
    person = _filer(ctx, str(ctx.params.get("person") or ""))
    account = str(ctx.params.get("account") or "")
    try:
        rows = json.loads(str(ctx.params.get("lines") or "[]"))
        moved = json.loads(str(ctx.params.get("transfers") or "[]"))
        lines = [(statements.Line.of(r), str(r.get("looks", ""))) for r in rows]
    except (ValueError, KeyError, TypeError):
        return JobOutput(status="ok", summary="expenses statement: not statement lines")
    if not person or account not in statements.ACCOUNTS:
        return JobOutput(status="ok", summary="expenses statement: nobody's account")
    by, said = str(ctx.params.get("by") or ""), str(ctx.params.get("said") or "")
    events = []
    for line, looks in lines:
        payload = line.payload(person, account) | {"by": by}
        if account == statements.OWN:
            payload |= {"looks": looks, "said": said}
        events.append(
            EventSpec(key=f"bank:{payload['sha256']}", event_type=LINE_EVENT, payload=payload)
        )
    kept = {e.payload["sha256"]: e.payload for e in events}
    for e in _events_of(ctx, LINE_EVENT):
        kept.setdefault(str(e["payload"].get("sha256")), e["payload"])
    for sha in moved:
        line = kept.get(str(sha))
        if line is None or line.get("account") != statements.MINISTRY:
            continue
        events.append(
            EventSpec(
                key=f"bank-transfer:{sha}",
                event_type=TRANSFER_EVENT,
                payload={"sha256": str(sha), "person": person, "day": line["day"]},
            )
        )
    return JobOutput(
        status="ok",
        summary=f"expenses statement: {len(lines)} line(s) of {person}'s {account} account kept",
        events=events,
    )


def _school_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expschool")
    for name in ("person", "rows", "by", "said"):
        key.param(name)
    key.config("expenses")
    return key.digest()


def _school_run(ctx: JobContext) -> JobOutput:
    """The person's school list (t-statements): each scholarship or school
    fee a line with no receipt, as they listed it."""
    person = _filer(ctx, str(ctx.params.get("person") or ""))
    try:
        rows = [
            statements.SchoolPayment(
                str(r["day"]),
                str(r["student"]),
                str(r["school"]),
                Decimal(str(r["amount"])),
                str(r["currency"]),
            )
            for r in json.loads(str(ctx.params.get("rows") or "[]"))
        ]
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return JobOutput(status="ok", summary="expenses school: not a school list")
    if not person:
        return JobOutput(status="ok", summary="expenses school: nobody's list")
    by, said = str(ctx.params.get("by") or ""), str(ctx.params.get("said") or "")
    events = [
        EventSpec(
            key=f"school:{r.sha(person)}",
            event_type=SCHOOL_EVENT,
            payload=r.payload(person) | {"by": by, "said": said},
        )
        for r in rows
    ]
    return JobOutput(
        status="ok", summary=f"expenses school: {len(rows)} payment(s) for {person}", events=events
    )


def _line_purpose_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "explinepurpose")
    sha = str(key.param("sha"))
    for name in ("said", "category", "by"):
        key.param(name)
    key.add("before", sum(1 for e in _events_of(ctx, LINE_PURPOSE_EVENT) if _payload_sha(e) == sha))
    key.config("expenses")
    return key.digest()


def _line_purpose_run(ctx: JobContext) -> JobOutput:
    """What one statement line was for, in the person's own words, and the
    category those words point to (t-statements)."""
    sha = str(ctx.params.get("sha") or "")
    said = str(ctx.params.get("said") or "").strip()
    if not said or not any(_payload_sha(e) == sha for e in _events_of(ctx, LINE_EVENT)):
        return JobOutput(status="ok", summary="expenses line purpose: no such line")
    before = sum(1 for e in _events_of(ctx, LINE_PURPOSE_EVENT) if _payload_sha(e) == sha)
    payload = {
        "sha256": sha,
        "said": said,
        "category": str(ctx.params.get("category") or ""),
        "by": str(ctx.params.get("by") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses line purpose: {sha[:12]} said",
        events=[
            EventSpec(
                key=f"bank-purpose:{sha}:{before}", event_type=LINE_PURPOSE_EVENT, payload=payload
            )
        ],
    )


JOBS: dict[str, JobHandler] = {
    "inbox": JobHandler(key=_inbox_key, run=_inbox_run),
    "intake": JobHandler(key=_intake_key, run=_intake_run),
    "extract": JobHandler(key=_extract_key, run=_extract_run),
    "report": JobHandler(key=_report_key, run=_report_run),
    "month": JobHandler(key=_month_key, run=_month_run),
    "date": JobHandler(key=_date_key, run=_date_run),
    "categorize": JobHandler(key=_categorize_key, run=_categorize_run),
    "drive": JobHandler(key=_drive_key, run=_drive_run),
    "drive_purpose": JobHandler(key=_purpose_key, run=_purpose_run),
    "amount": JobHandler(key=_amount_key, run=_amount_run),
    "statement": JobHandler(key=_statement_key, run=_statement_run),
    "school": JobHandler(key=_school_key, run=_school_run),
    "line_purpose": JobHandler(key=_line_purpose_key, run=_line_purpose_run),
    "send": JobHandler(key=_send_key, run=_send_run),
    "match": JobHandler(key=_match_key, run=_match_run),
}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {
    INBOX_FILE_CARD: CardRule("approve", "expense.report", money=False),
    INBOX_SKIP_CARD: CardRule("approve", "expense.report", money=False),
    REVIEW_CARD: CardRule(
        "approve", "expense.report", amount="total_cents", cents=True, submitter="person"
    ),
    DRAFT_CARD: CardRule("approve", "expense.report", submitter="person"),
    # Sending claims commits none of the ministry's money: the institution
    # approves and pays in its own system. The person decides their own.
    SEND_CARD: CardRule("submit", "expense.report", money=False, submitter="person"),
    CONFIRM_CARD: CardRule(
        "approve", "payment", amount="total_cents", cents=True, submitter="person"
    ),
    "expenses.duplicate_drop": CardRule("approve", "expense.report", money=False),
    "expenses.attribute_receipt": CardRule("approve", "expense.report", money=False),
    "expenses.unknown_person_folder": CardRule("approve", "expense.report", money=False),
    "expenses.match_ambiguous": CardRule("approve", "expense.report", money=False),
    "expenses.qbo_map_payee": CardRule("approve", "books", money=False),
    "expenses.qbo_map_project_account": CardRule("approve", "books", money=False),
    "expenses.qbo_map_category": CardRule("approve", "books", money=False),
    "expenses.qbo_duplicate_review": CardRule("approve", "books", money=False),
}
