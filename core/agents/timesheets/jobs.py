"""Timesheets agent jobs: intake.

Deterministic end to end: filename pattern gates candidacy, the CSV parser is
pure code, and hours land in an approval card, never in a payroll system
(invariant 7). See ``brief.md``; the parser contract lives in ``schema.py``.
"""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path

from ...engine.config import MissingFolderError
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobHandler, JobOutput
from ...engine.fileops import CannotVerify, CopyMismatch, place_copy
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from .schema import TimesheetParseError, is_timesheet_name, parse_timesheet_csv

RECORDED_EVENTS = {"timesheets.recorded", "timesheets.companion_filed", "timesheets.flagged"}


def _landing_dir(ctx: JobContext) -> Path:
    configured = ctx.params.get("landing_dir") or ctx.tenant.timesheets.landing_dir
    if not configured:
        raise ValueError(
            "no landing directory: set [timesheets].landing_dir in tenant.toml "
            "or pass --param landing_dir=PATH"
        )
    return Path(configured)


def _filing_dir(ctx: JobContext) -> Path:
    configured = ctx.params.get("filing_dir") or ctx.tenant.timesheets.filing_dir
    if not configured:
        raise ValueError(
            "no filing directory: set [timesheets].filing_dir in tenant.toml "
            "or pass --param filing_dir=PATH"
        )
    return Path(configured)


def _md5(path: Path) -> str | None:
    """Hex MD5, or ``None`` for a cloud-only placeholder (same contract as
    AP's hasher; see docs/lessons.md, "A cloud placeholder is not here yet")."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        if exc.errno == errno.EDEADLK:
            return None
        raise
    return hashlib.md5(data).hexdigest()


def _candidates(ctx: JobContext) -> list[Path]:
    landing = _landing_dir(ctx)
    if not landing.is_dir():
        raise MissingFolderError(f"landing directory {landing} does not exist")
    return [p for p in sorted(landing.iterdir()) if p.is_file() and is_timesheet_name(p.name)]


def _intake_key(ctx: JobContext) -> str:
    """Same set of timesheet files (names + content) = same run = no-op."""
    key = RunKey(ctx, "timesheets")
    key.param("filing_dir")
    key.config("timesheets")
    key.files(_candidates(ctx), digest=_md5)
    return key.digest()


def _already_seen_md5s(ctx: JobContext) -> set[str]:
    seen: set[str] = set()
    for e in ctx.ledger.read_event_log():
        if e.get("event_type") in RECORDED_EVENTS:
            md5 = str(e.get("idempotency_key", "")).rsplit(":", 1)[-1]
            if len(md5) == 32:
                seen.add(md5)
    return seen


def _file_copy(src: Path, dest_dir: Path, *, guard, shadow: bool) -> Path:
    """Guarded, never-overwriting, read-back-verified copy into the filing
    month folder (the core.engine.fileops contract, honesty audit 2026-09-03
    03-F4/03-F5): a cloud-only placeholder on either side raises
    CannotVerify for the caller to defer; it used to read None != x as a
    different file and land a duplicate."""
    return place_copy(src, dest_dir / src.name, guard=guard, shadow=shadow, hash_fn=_md5).dest


def _intake_run(ctx: JobContext) -> JobOutput:
    filing = _filing_dir(ctx)
    seen = _already_seen_md5s(ctx)

    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []
    by_stem: dict[str, dict[str, Path]] = {}
    for path in _candidates(ctx):
        by_stem.setdefault(path.stem, {})[path.suffix.lower()] = path

    recorded = flagged = 0
    for _stem, pair in sorted(by_stem.items()):
        csv_path = pair.get(".csv")
        if csv_path is None:
            continue  # an orphan xlsx waits for its csv (or the age rule)
        md5 = _md5(csv_path)
        if md5 is None:
            anomalies.append(
                Anomaly(
                    code="timesheets.cloud_only_deferred",
                    detail=f"{csv_path.name}: cloud-only placeholder; the submission "
                    "waits until it materializes",
                )
            )
            continue
        if md5 in seen:
            continue
        try:
            sub = parse_timesheet_csv(csv_path.read_text(encoding="utf-8", errors="replace"))
        except TimesheetParseError as exc:
            flagged += 1
            anomalies.append(
                Anomaly(code="timesheets.parse_failed", detail=f"{csv_path.name}: {exc}")
            )
            if ctx.shadow:
                actions.append(f"would flag {csv_path.name}: {exc}")
                continue
            events.append(
                EventSpec(
                    key=f"tsflag:{md5}",
                    event_type="timesheets.flagged",
                    payload={"file": csv_path.name, "reason": str(exc)},
                )
            )
            continue

        # Honesty audit 2026-09-03 (03-F5): the pair files together or waits
        # together. A cloud-only companion used to be skipped silently and
        # could never be filed later (the csv's event made the pair "seen").
        xlsx_path = pair.get(".xlsx")
        xmd5 = _md5(xlsx_path) if xlsx_path is not None else None
        if xlsx_path is not None and xmd5 is None:
            anomalies.append(
                Anomaly(
                    code="timesheets.cloud_only_deferred",
                    detail=f"{xlsx_path.name}: companion is a cloud-only placeholder; "
                    "the pair waits until it materializes",
                )
            )
            continue
        month_dir = filing / sub.month
        try:
            dest = _file_copy(csv_path, month_dir, guard=ctx.guard, shadow=ctx.shadow)
            xdest = (
                _file_copy(xlsx_path, month_dir, guard=ctx.guard, shadow=ctx.shadow)
                if xlsx_path is not None
                else None
            )
        except CannotVerify as exc:
            anomalies.append(
                Anomaly(
                    code="timesheets.cloud_only_deferred",
                    detail=f"{csv_path.name}: cannot verify the filing {exc.side} "
                    f"({Path(exc.filename).name} is a cloud-only placeholder); the pair waits",
                )
            )
            continue
        except CopyMismatch as exc:
            anomalies.append(
                Anomaly(
                    code="timesheets.copy_unverified",
                    detail=f"{csv_path.name}: the copy to {exc.dest.name} read back different "
                    "content; removed, retried next run",
                )
            )
            continue
        # Honesty audit 2026-09-03 (03-F2): shadow says what it would do and
        # returns before any event or card. It used to record `recorded`
        # events with a `filed_to` never written and park a real payroll
        # card; the next real run then read the pair as seen and never filed
        # it, and the AP janitor archived the originals.
        if ctx.shadow:
            recorded += 1
            actions.append(f"would file {csv_path.name} -> {month_dir}")
            if xlsx_path is not None:
                actions.append(f"would file {xlsx_path.name} -> {month_dir}")
            actions.append(
                f"would park hours card for {sub.person} w/e {sub.week_ending}: "
                f"{sub.total_hours:g}h"
            )
            continue
        events.append(
            EventSpec(
                key=f"ts:{md5}",
                event_type="timesheets.recorded",
                payload={
                    "file": csv_path.name,
                    "person": sub.person,
                    "week_ending": sub.week_ending,
                    "submitted_at": sub.submitted_at,
                    "total_hours": sub.total_hours,
                    "lines": [line.model_dump() for line in sub.lines],
                    "filed_to": str(dest),
                },
            )
        )
        if xlsx_path is not None:
            events.append(
                EventSpec(
                    key=f"tsx:{xmd5}",
                    event_type="timesheets.companion_filed",
                    payload={"file": xlsx_path.name, "filed_to": str(xdest)},
                )
            )
        approvals.append(
            ApprovalSpec(
                key=f"ts:{md5}",
                action_type="timesheets.payroll_hours",
                params={
                    "person": sub.person,
                    "week_ending": sub.week_ending,
                    "total_hours": sub.total_hours,
                    "breakdown": "; ".join(f"{line.project} {line.hours:g}h" for line in sub.lines),
                    "file": csv_path.name,
                },
                reason="hours for the next pay run; approval records the owner's review",
            )
        )
        recorded += 1
        actions.append(f"recorded {sub.person} w/e {sub.week_ending}: {sub.total_hours:g}h")

    if ctx.shadow:
        summary = f"timesheets (shadow): would record {recorded}, flagged {flagged}"
    else:
        summary = f"timesheets: recorded {recorded}, flagged {flagged}"
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


JOBS: dict[str, JobHandler] = {
    "intake": JobHandler(key=_intake_key, run=_intake_run),
}
