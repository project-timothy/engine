"""Lens 18 — projects: every PN the ledger references is a real project.

The old bookkeeper-auditor's A-PJ-001, re-founded on the ledger
(2026-09-04). PN identity is the money boundary: per-project COGS drives
profit shares and the year-end picture, and the engine has no PN
allowlist (the registry default gl_account and the owner's edit at
approval are trusted as typed). This lens collects every PN referenced by

- AP rows: the P-number in ``gl_account`` (the house
  ``... - PNYY_NNNN`` subaccount spelling) and the ``project`` field;
- expense lines: ``expense_line.project``;
- timesheet lines: ``timesheets.recorded`` events' ``lines[].project``;

resolves each through the tenant-configured canonicalizer
(``auditor/pn.py``: four written forms, nicknames from
``[auditor.projects].nicknames``), and checks the set against the project
registry TOML's ``[[project]].pn`` list. A PN absent from the registry is
``orphan-pn`` (WARN, one per PN, naming where it was seen); a project
string that resolves to nothing is ``unresolved-project`` (INFO). Overhead
and multi-project sentinels are neither. Read-only.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ..findings import Finding
from ..pn import MULTI, OVERHEAD, CodeFormat, canonicalize, find_pns
from . import AuditContext
from ._tables import has_table

LENS = "projects"
_MAX_LISTED = 5


def _registry_pns(path: Path) -> set[str] | None:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return {str(p.get("pn", "")).strip() for p in raw.get("project", []) if p.get("pn")}


def _collect(ctx: AuditContext) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(canonical PN -> sightings, unresolvable text -> sightings)."""
    slug = ctx.tenant.slug
    nicknames = ctx.tenant.projects_nicknames
    overhead = ctx.tenant.projects_overhead_tokens
    code = CodeFormat(ctx.tenant.cost_object_pattern, ctx.tenant.cost_object_canonical)
    pns: dict[str, list[str]] = {}
    unresolved: dict[str, list[str]] = {}

    def note(value: str | None, where: str) -> None:
        text = (value or "").strip()
        if not text:
            return
        canon = canonicalize(text, nicknames=nicknames, overhead_tokens=overhead, code=code)
        if canon in (OVERHEAD, MULTI):
            return
        if canon is None:
            unresolved.setdefault(text, []).append(where)
        else:
            pns.setdefault(canon, []).append(where)

    if has_table(ctx, "ap_invoices"):
        for row in ctx.ledger.query(
            "SELECT id, vendor, invoice_number, gl_account, project FROM ap_invoices "
            "WHERE tenant = ? ORDER BY id",
            (slug,),
        ):
            label = f"AP #{row['id']} {row['vendor']} / {row['invoice_number']}"
            for pn in find_pns(row["gl_account"], code):
                pns.setdefault(pn, []).append(f"{label} (gl_account)")
            note(row["project"], f"{label} (project)")
    if has_table(ctx, "expense_line") and has_table(ctx, "expense_report"):
        for row in ctx.ledger.query(
            "SELECT l.id, l.report_id, l.project, r.person FROM expense_line l "
            "JOIN expense_report r ON r.id = l.report_id WHERE l.tenant = ? ORDER BY l.id",
            (slug,),
        ):
            note(
                row["project"],
                f"expense report #{row['report_id']} {row['person']} line #{row['id']}",
            )
    if has_table(ctx, "events"):
        for row in ctx.ledger.query(
            "SELECT payload_json FROM events WHERE tenant = ? "
            "AND event_type = 'timesheets.recorded' ORDER BY id",
            (slug,),
        ):
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            label = f"timesheet {payload.get('person', '?')} w/e {payload.get('week_ending', '?')}"
            for line in payload.get("lines") or []:
                if isinstance(line, dict):
                    note(str(line.get("project") or ""), label)
    return pns, unresolved


def _seen(where: list[str]) -> str:
    listed = "; ".join(where[:_MAX_LISTED])
    more = f"; and {len(where) - _MAX_LISTED} more" if len(where) > _MAX_LISTED else ""
    return f"seen in: {listed}{more}"


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.projects_enabled or not ctx.tenant.registry_path:
        return []
    path = Path(ctx.tenant.registry_path).expanduser()
    registered = _registry_pns(path) if path.is_file() else None
    if registered is None:
        return [
            Finding(
                lens=LENS,
                subject="project registry",
                condition="registry-missing",
                severity="WARN",
                detail=f"the project registry {path} could not be read; no PN was checked tonight",
            )
        ]
    pns, unresolved = _collect(ctx)
    findings: list[Finding] = []
    for pn, where in sorted(pns.items()):
        if pn in registered:
            continue
        findings.append(
            Finding(
                lens=LENS,
                subject=pn,
                condition="orphan-pn",
                severity="WARN",
                detail=f"{pn} is not in the project registry ({path.name}, "
                f"{len(registered)} registered); {_seen(where)}; a typo codes money to a "
                "project that does not exist: correct the rows or register the project",
            )
        )
    for text, where in sorted(unresolved.items()):
        findings.append(
            Finding(
                lens=LENS,
                subject=text,
                condition="unresolved-project",
                severity="INFO",
                detail=f"'{text}' reads as no PN, overhead bucket, or configured nickname; "
                f"{_seen(where)}; add a nickname under [auditor.projects] or recode the row",
            )
        )
    return findings
