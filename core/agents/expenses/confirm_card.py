"""The reimbursement confirm card: its state and spec, and recovery of a
built report whose card never parked. The report and match jobs share it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from .common import _events_of, _filing_dir, _landed_by_sha, _person_named
from .records import (
    STATUS_OPEN,
)

CONFIRM_CARD = "expenses.reimbursement_record"
BUILT_EVENT = "expense.report_built"


def _channel_for(ctx: JobContext, person_name: str) -> str:
    """The payment channel named on this person's confirm cards: the
    person-level override when set, else the tenant default (issue #120)."""
    person = _person_named(ctx, person_name)
    override = getattr(person, "default_channel", "") if person else ""
    return override or ctx.tenant.expenses.default_channel


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


def _reports(ctx: JobContext, status: str) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM expense_report WHERE tenant = ? AND status = ? ORDER BY id",
        (ctx.tenant_slug, status),
    ).fetchall()
    return [dict(r) for r in rows]


def _lines_for(ctx: JobContext, report_id: int) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM expense_line WHERE report_id = ? ORDER BY id", (report_id,)
    ).fetchall()
    return [dict(r) for r in rows]
