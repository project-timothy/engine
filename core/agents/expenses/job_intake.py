"""The intake job: receipts dropped in a person's folder are split when a scan
holds several, filed by hash, and landed as events.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.fileops import CannotVerify, CopyMismatch
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from .common import (
    _drop_dir,
    _events_of,
    _file_copy,
    _filing_dir,
    _landed_by_sha,
    _persons,
    _placement_anomaly,
    _sha256,
    _tenant_month,
)
from .records import (
    LANDED_EVENT,
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
    parse_project_tag,
)

SPLIT_EVENT = "expense.scan_split"
SINGLE_EVENT = "expense.scan_single"
# Job-time durable records (honesty audit #170, 03-F9): written BEFORE a
# file moves, linked to the run when it lands, healed into the event above
# on the next run if the run died in between.
SPLIT_RECORD = "expense.scan_split.recorded"
UNKNOWN_FOLDER_EVENT = "expense.unknown_person_folder"


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
