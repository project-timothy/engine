"""Lens 8 — timesheets: every submission became hours in front of the owner.

The engine's timesheet vertical has one promise: a submission lands, parses
deterministically, files to the Timesheets tree, and queues exactly one
payroll-hours card for the owner (entering hours in the payroll system stays
the owner's act). Three checks, timesheet-shaped files only:

- every ``timesheets.recorded`` event has its payroll-hours card — recorded
  hours that never reached the owner's queue are hours nobody will pay;
- every filed submission in the Timesheets tree has its recorded event (a
  file the events cannot vouch for was placed by something else);
- no submission sits at the landing top level past the grace window with no
  timesheets event at all — arrived, and the vertical never noticed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..findings import Finding
from . import AuditContext
from .filing import tree_missing

LENS = "timesheets"

RECORDED_EVENT = "timesheets.recorded"
COMPANION_EVENT = "timesheets.companion_filed"
FLAGGED_EVENT = "timesheets.flagged"
PAYROLL_CARD = "timesheets.payroll_hours"


def _is_timesheet_name(name: str) -> bool:
    return name.lower().startswith("timesheet")


def _events(ctx: AuditContext, event_types: tuple[str, ...]) -> list[dict]:
    placeholders = ",".join("?" for _ in event_types)
    payloads = []
    for row in ctx.ledger.query(
        f"SELECT event_type, payload_json FROM events WHERE tenant = ? "
        f"AND event_type IN ({placeholders}) ORDER BY id",
        (ctx.tenant.slug, *event_types),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        payload["_event_type"] = row["event_type"]
        payloads.append(payload)
    return payloads


def _card_files(ctx: AuditContext) -> set[str]:
    files: set[str] = set()
    for row in ctx.ledger.query(
        "SELECT params_json FROM approval_queue WHERE tenant = ? AND action_type = ?",
        (ctx.tenant.slug, PAYROLL_CARD),
    ):
        try:
            params = json.loads(row["params_json"])
        except json.JSONDecodeError:
            continue
        name = str(params.get("file", ""))
        if name:
            files.add(name)
    return files


def _age_days(path: Path, now: datetime) -> float:
    return (now.timestamp() - path.stat().st_mtime) / 86400


def _is_legacy(path: Path, coverage_since: str) -> bool:
    if not coverage_since:
        return False
    cutoff = datetime.fromisoformat(coverage_since + "T00:00:00+00:00").timestamp()
    return path.stat().st_mtime < cutoff


def check(ctx: AuditContext) -> list[Finding]:
    findings: list[Finding] = []
    events = _events(ctx, (RECORDED_EVENT, COMPANION_EVENT, FLAGGED_EVENT))
    recorded = [p for p in events if p["_event_type"] == RECORDED_EVENT]
    event_files = {str(p.get("file", "")) for p in events}
    cards = _card_files(ctx)

    for payload in recorded:
        name = str(payload.get("file", ""))
        if name and name not in cards:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=name,
                    condition="hours-never-queued",
                    severity="WARN",
                    detail="the submission was recorded and filed but no payroll-hours "
                    "card exists; these hours never reached the owner's queue",
                )
            )

    filing_dir = (
        Path(ctx.tenant.timesheets_filing_dir).expanduser()
        if ctx.tenant.timesheets_filing_dir
        else None
    )
    if filing_dir is not None and not filing_dir.is_dir():
        findings.append(tree_missing(LENS, "Timesheets tree", filing_dir))  # 04-F4
    elif filing_dir is not None:
        for path in sorted(filing_dir.rglob("timesheet*")):
            if not path.is_file() or not _is_timesheet_name(path.name):
                continue
            if _is_legacy(path, ctx.tenant.coverage_since):
                continue
            if path.name not in event_files:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=path.name,
                        condition="filed-without-record",
                        severity="WARN",
                        detail=f"sits in {path.parent.name}/ but no timesheets event "
                        "vouches for it; something other than the engine filed it",
                    )
                )

    landing = Path(ctx.tenant.landing_dir).expanduser() if ctx.tenant.landing_dir else None
    if landing is not None and not landing.is_dir():
        findings.append(tree_missing(LENS, "landing folder", landing))  # 04-F4
    elif landing is not None:
        for path in sorted(landing.iterdir()):
            if not path.is_file() or not _is_timesheet_name(path.name):
                continue
            if _is_legacy(path, ctx.tenant.coverage_since):
                continue
            if path.name in event_files:
                continue
            age = _age_days(path, ctx.now)
            if age >= ctx.tenant.filing_grace_days:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=path.name,
                        condition="stuck-unrecorded",
                        severity="WARN",
                        detail=f"a submission has sat at the landing top level for "
                        f"{age:.0f} day(s) with no timesheets event; the vertical "
                        "never noticed it",
                    )
                )
    return findings
