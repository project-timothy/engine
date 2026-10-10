"""Projects agent jobs: drift.

Reads the newest project-registry drift report and turns each item into one
approval card the owner answers once (#325, the 2026-09-19 proposal). Off
behind ``[projects].drift_cards`` until the owner turns it on.

On approval:
- ``one-source`` and ``new-since-sync``: a decision only. The engine never
  writes the registry file; the run says the line to add.
- ``folder-missing``: ``mkdir`` of the project folder under
  ``[projects].folder_root`` (a write-guard carve-out), create only.
- ``qbo-project-missing``: the owner's act in the accounting UI for now. The
  API create (``drift.execute_project_create``, readback-verified) waits on
  its live probe, which needs a real missing project to prove.
A rejected card is a permanent answer: the item never cards again.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...authority import CardRule
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobHandler, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from .drift import (
    DECISION_ONLY,
    DRIFT_ACTION,
    DriftItem,
    parse_report,
    project_display_name,
    propose_cards,
)

DONE_EVENT = "projects.drift_answered"


def _report(ctx: JobContext) -> Path | None:
    given = ctx.params.get("report")
    if given:
        return Path(given)
    registry = ctx.tenant.projects.registry_path
    if not registry:
        return None
    reports = sorted(Path(registry).expanduser().parent.glob(ctx.tenant.projects.drift_glob))
    return reports[-1] if reports else None


def _cards(ctx: JobContext) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ?",
        (ctx.tenant_slug, DRIFT_ACTION),
    ).fetchall()
    return [
        {"id": r["id"], "status": r["status"], **json.loads(r["params_json"] or "{}")} for r in rows
    ]


def _answered_card_ids(ctx: JobContext) -> set[int]:
    rows = ctx.ledger.conn.execute(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant_slug, DONE_EVENT),
    ).fetchall()
    return {int(json.loads(r["payload_json"] or "{}").get("card", -1)) for r in rows}


def _enabled(ctx: JobContext) -> bool:
    given = ctx.params.get("drift_cards")
    return (
        str(given).lower() in ("1", "true", "yes")
        if given is not None
        else ctx.tenant.projects.drift_cards
    )


def _key(ctx: JobContext) -> str:
    key = RunKey(ctx, "projects-drift")
    key.param("report")
    key.param("drift_cards")
    key.param("folder_root")
    key.config("projects")
    report = _report(ctx)
    key.add("report", report.read_bytes().hex() if report and report.is_file() else "none")
    key.value("cards", sorted((c["id"], c["status"]) for c in _cards(ctx)))
    key.value("answered", sorted(_answered_card_ids(ctx)))
    return key.digest()


def _execute(ctx: JobContext, card: dict) -> tuple[list[str], list[EventSpec], list[Anomaly]]:
    item = DriftItem(
        pn=card["pn"], name=card.get("name", ""), kind=card["kind"], text=card.get("text", "")
    )
    label = project_display_name(item)
    if item.kind in DECISION_ONLY:
        act = f"add {label} to the project registry (the engine never writes that file)"
    elif item.kind == "folder-missing":
        root = ctx.params.get("folder_root") or ctx.tenant.projects.folder_root
        if not root:
            return (
                [],
                [],
                [
                    Anomaly(
                        code="projects.no_folder_root",
                        detail=f"{label}: set [projects].folder_root",
                    )
                ],
            )
        folder = Path(root).expanduser() / label
        ctx.guard.check_write(folder)
        folder.mkdir(parents=True, exist_ok=True)
        act = f"folder created: {folder}"
    else:  # qbo-project-missing
        act = (
            f"create the QBO Project {label!r} in the accounting UI "
            "(the API create awaits its live probe)"
        )
    event = EventSpec(
        key=f"answered:{card['id']}",
        event_type=DONE_EVENT,
        payload={"card": card["id"], "pn": item.pn, "kind": item.kind, "act": act},
    )
    return [act], [event], []


def _drift_run(ctx: JobContext) -> JobOutput:
    if not _enabled(ctx):
        return JobOutput(
            status="ok", summary="projects drift: off ([projects].drift_cards = false)"
        )
    report = _report(ctx)
    if report is None or not report.is_file():
        return JobOutput(status="ok", summary="projects drift: no drift report found")
    items = parse_report(report.read_text(encoding="utf-8", errors="replace"))
    cards = _cards(ctx)
    actions: list[str] = []
    events: list[EventSpec] = []
    anomalies = [
        Anomaly(code="projects.drift_unclassified", detail=f"{i.text} (reported, not acted on)")
        for i in items
        if i.kind == "unclassified"
    ]
    if not ctx.shadow:
        done = _answered_card_ids(ctx)
        for card in cards:
            if card["status"] == "approved" and card["id"] not in done:
                a, e, an = _execute(ctx, card)
                actions += a
                events += e
                anomalies += an
    known = frozenset(f"{c['pn']}:{c['kind']}" for c in cards)
    new = propose_cards(items, answered=known)
    if ctx.shadow:  # a dry run reports what it would ask; it parks nothing
        return JobOutput(
            status="ok",
            summary=f"projects drift: {report.name}, {len(items)} item(s); would park "
            f"{len(new)} card(s)",
            anomalies=anomalies,
        )
    summary = (
        f"projects drift: {report.name}, {len(items)} item(s); "
        f"{len(new)} new card(s), {len(actions)} answer(s) carried out"
    )
    approvals = [
        ApprovalSpec(
            key=f"drift:{c.key}",
            action_type=DRIFT_ACTION,
            params={"pn": c.item.pn, "name": c.item.name, "kind": c.item.kind, "text": c.item.text},
            reason=f"{c.item.text} [{c.item.kind}]",
        )
        for c in new
    ]
    return JobOutput(
        status="needs_approval" if approvals else "ok",
        summary=summary,
        actions=actions,
        events=events,
        anomalies=anomalies,
        approvals=approvals,
    )


JOBS: dict[str, JobHandler] = {"drift": JobHandler(key=_key, run=_drift_run)}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {
    "projects.registry_drift": CardRule("approve", "books", money=False)
}
