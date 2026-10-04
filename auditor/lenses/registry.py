"""Lens 17 — registry freshness: the project registry is alive and clean.

The project registry TOML the tenant names in ``[auditor.registry].path``
is written by a sync job outside the engine (weekly, from several
sources) and read by the projects lens as the list of real PNs. Two
things can go quietly wrong with a file nobody opens: the sync stops (the
file's mtime ages past ``max_age_hours``, 168 by default, one week plus
slack) and the sync keeps reporting drift nobody reads (the newest
``project-registry-drift-*.md`` beside the file lists items). Read-only.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..findings import Finding
from . import AuditContext

LENS = "registry"

_DRIFT_GLOB = "project-registry-drift-*.md"
_DRIFT_COUNT = re.compile(r"\*\*Drift items:\*\*\s*(\d+)")
_MAX_LISTED = 6


def _drift_items(text: str) -> tuple[int, list[str]]:
    bullets = [
        line.strip()[2:].strip() for line in text.splitlines() if line.strip().startswith("- ")
    ]
    m = _DRIFT_COUNT.search(text)
    count = int(m.group(1)) if m else len(bullets)
    return count, bullets


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.registry_enabled or not ctx.tenant.registry_path:
        return []
    path = Path(ctx.tenant.registry_path).expanduser()
    if not path.is_file():
        return [
            Finding(
                lens=LENS,
                subject="project registry",
                condition="registry-missing",
                severity="WARN",
                detail=f"the configured project registry {path} does not exist; "
                "the PN list could not be read tonight",
            )
        ]
    findings: list[Finding] = []
    age_hours = (ctx.now.timestamp() - path.stat().st_mtime) / 3600
    if age_hours > ctx.tenant.registry_max_age_hours:
        findings.append(
            Finding(
                lens=LENS,
                subject="project registry",
                condition="registry-stale",
                severity="WARN",
                detail=f"{path.name} was last written {age_hours:.0f}h ago (window "
                f"{ctx.tenant.registry_max_age_hours}h); the registry sync has stopped",
            )
        )
    drifts = sorted(path.parent.glob(_DRIFT_GLOB))
    if drifts:
        newest = drifts[-1]
        count, items = _drift_items(newest.read_text(encoding="utf-8", errors="replace"))
        if count > 0:
            listed = "; ".join(items[:_MAX_LISTED])
            more = f"; and {len(items) - _MAX_LISTED} more" if len(items) > _MAX_LISTED else ""
            findings.append(
                Finding(
                    lens=LENS,
                    subject="project registry drift",
                    condition="registry-drift",
                    severity="INFO",
                    detail=f"{newest.name} lists {count} item(s): {listed}{more}",
                )
            )
    return findings
