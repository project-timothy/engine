"""Lens 2 — filing coverage: every arrival has a story.

Walks the landing folder (top level and ``_archive/``) itself and demands a
disposition in the ledger for every file: some event or approval card whose
payload mentions it (recorded, routed, skipped, archived, saved, or pending
a decision). A file with no story is the lens that catches "something
arrived and nothing noticed". Top-level items older than the decision
window are stalled even when noticed: the top level is the owner's inbox.

Files whose mtime predates ``coverage_since`` are legacy — they arrived
before the engine owned the folder and the event log cannot vouch for them.

An unconfigured landing folder is out of scope; a CONFIGURED one that is not
there is a ``tree-missing`` WARN (honesty audit 2026-09-03, 04-F4): the
lens must never go green over a folder it could not read.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePath

from ..findings import Finding
from . import AuditContext

LENS = "filing"

_IGNORED_NAMES = {".DS_Store", "Icon\r", "desktop.ini"}


def _mentioned_basenames(ctx: AuditContext) -> set[str]:
    """Every basename any event payload or approval card mentions."""
    mentioned: set[str] = set()

    def harvest(value) -> None:
        if isinstance(value, str):
            if value:
                mentioned.add(value)
                tail = PurePath(value).name
                if tail:
                    mentioned.add(tail)
        elif isinstance(value, dict):
            for v in value.values():
                harvest(v)
        elif isinstance(value, list):
            for v in value:
                harvest(v)

    for row in ctx.ledger.query(
        "SELECT payload_json FROM events WHERE tenant = ?", (ctx.tenant.slug,)
    ):
        try:
            harvest(json.loads(row["payload_json"]))
        except json.JSONDecodeError:
            continue
    for row in ctx.ledger.query(
        "SELECT params_json FROM approval_queue WHERE tenant = ?", (ctx.tenant.slug,)
    ):
        try:
            harvest(json.loads(row["params_json"]))
        except json.JSONDecodeError:
            continue
    return mentioned


def _age_days(path: Path, now: datetime) -> float:
    return (now.timestamp() - path.stat().st_mtime) / 86400


def _is_legacy(path: Path, coverage_since: str) -> bool:
    if not coverage_since:
        return False
    cutoff = datetime.fromisoformat(coverage_since + "T00:00:00+00:00").timestamp()
    return path.stat().st_mtime < cutoff


def tree_missing(lens: str, label: str, path: Path) -> Finding:
    """The shared shape for 'configured but not there' (04-F4): one WARN per
    tree, so a renamed or unmounted folder is a checklist item, not silence."""
    return Finding(
        lens=lens,
        subject=f"{label} {path}",
        condition="tree-missing",
        severity="WARN",
        detail=f"the configured {label} {path} does not exist or is not a directory "
        "(renamed, unmounted, or evicted); nothing under it was checked tonight",
    )


def check(ctx: AuditContext) -> list[Finding]:
    landing = Path(ctx.tenant.landing_dir).expanduser() if ctx.tenant.landing_dir else None
    if landing is None:
        return []  # tenant has no landing folder to audit
    if not landing.is_dir():
        return [tree_missing(LENS, "landing folder", landing)]
    findings: list[Finding] = []
    mentioned = _mentioned_basenames(ctx)

    top_level = [p for p in sorted(landing.iterdir()) if p.is_file()]
    archived = [p for p in sorted((landing / "_archive").rglob("*")) if p.is_file()]

    for path in [*top_level, *archived]:
        if path.name in _IGNORED_NAMES or path.name.startswith("."):
            continue
        if _is_legacy(path, ctx.tenant.coverage_since):
            continue
        age = _age_days(path, ctx.now)
        if path.name not in mentioned and age >= ctx.tenant.filing_grace_days:
            where = "the archive" if path.parent != landing else "the landing folder"
            findings.append(
                Finding(
                    lens=LENS,
                    subject=path.name,
                    condition="no-disposition",
                    severity="WARN",
                    detail=f"sitting in {where} for {age:.0f} day(s) with no trace in "
                    "the ledger: nothing recorded, routed, skipped, archived, or queued",
                )
            )

    for path in top_level:
        if path.name in _IGNORED_NAMES or path.name.startswith("."):
            continue
        if _is_legacy(path, ctx.tenant.coverage_since):
            continue
        age = _age_days(path, ctx.now)
        if age > ctx.tenant.decision_window_days:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=path.name,
                    condition="stalled-at-top-level",
                    severity="WARN",
                    detail=f"still at the landing folder's top level after {age:.0f} day(s) "
                    f"(decision window {ctx.tenant.decision_window_days}d); it is waiting "
                    "on someone and nothing is moving",
                )
            )
    return findings
