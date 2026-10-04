"""Lens 16 — triage: the morning loop's own markers, and dated decisions.

Two checks, both local:

- **The headless marker.** The 06:00 triage wrapper writes its own note
  when it cannot run (``... headless run SKIPPED`` / ``FAILED`` as the
  first line; the strings live in the tenant's wrapper script). Three such
  markers in a row went unread in 2026-09 (09-02, 09-03, 09-04) because
  nothing put them on a report. The newest note under the configured
  notes folder is read; a marker is ``triage-skipped`` / ``triage-failed``
  (WARN) carrying the wrapper's reason, and it resolves the moment a real
  note is newer. The notes folder is explicit config: unset means the
  tenant runs no headless triage and the check is out of scope; set and
  absent is ``tree-missing``.
- **Aged decisions.** Dated triage.toml entries (``since = ...`` on a mute
  or an acknowledgment) older than ``ack_max_age_days`` (90) are reported
  once as ``acknowledgment-aged`` (INFO): re-confirm by bumping the date,
  or delete the entry to resume surveillance. The mute keeps applying
  meanwhile (one line, never a flood of un-muted items). Undated entries
  never age; the original permanent semantics stand.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..findings import Finding
from ..triage import load_triage
from . import AuditContext
from .filing import tree_missing

LENS = "triage"

_NOTE = re.compile(r"^triage-(\d{4}-\d{2}-\d{2})\.md$")
SKIPPED_SUFFIX = "headless run SKIPPED"
FAILED_SUFFIX = "headless run FAILED"


def _newest_note(notes_dir: Path) -> tuple[str, Path] | None:
    notes = [(m.group(1), p) for p in notes_dir.iterdir() if (m := _NOTE.match(p.name))]
    return max(notes) if notes else None


def check_marker(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.triage_notes_dir:
        return []
    notes_dir = Path(ctx.tenant.triage_notes_dir).expanduser()
    if not notes_dir.is_dir():
        return [tree_missing(LENS, "triage notes folder", notes_dir)]
    newest = _newest_note(notes_dir)
    if newest is None:
        return []
    day, path = newest
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    first = lines[0].strip() if lines else ""
    if first.endswith(SKIPPED_SUFFIX):
        condition = "triage-skipped"
    elif first.endswith(FAILED_SUFFIX):
        condition = "triage-failed"
    else:
        return []
    reason = next((line.strip() for line in lines[1:] if line.strip()), "(no reason given)")
    return [
        Finding(
            lens=LENS,
            subject=f"triage note {day}",
            condition=condition,
            severity="WARN",
            detail=f"the headless triage wrapper left only its marker in {path.name}: "
            f"{reason} Run /audit-triage interactively to close that day's loop",
        )
    ]


def check_aged_entries(ctx: AuditContext) -> list[Finding]:
    triage = load_triage(ctx.tenant.slug, tenants_dir=ctx.tenants_dir, as_of=ctx.now.date())
    findings: list[Finding] = []
    for entry in triage.aged_entries(max_age_days=ctx.tenant.triage_ack_max_age_days):
        findings.append(
            Finding(
                lens=LENS,
                subject=f"{entry.kind} {entry.key}",
                condition="acknowledgment-aged",
                severity="INFO",
                detail=f"the triage.toml {entry.kind} '{entry.key}' is dated "
                f"{entry.since.isoformat()}, {entry.age_days} days ago (acknowledgment "
                "aged out; re-confirm or delete): bump its since date to keep it, or "
                "delete the entry to resume surveillance",
            )
        )
    return findings


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.triage_enabled:
        return []
    return [*check_marker(ctx), *check_aged_entries(ctx)]
