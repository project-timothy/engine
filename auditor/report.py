"""The nightly report: a running checklist, never a re-alarm.

Three sections in a fixed order (docs/auditor-design.md): NEW is the only
section that asks for the owner's attention; the open checklist reads like a
to-do list because it is one; resolved is the quiet record that the list is
shrinking. An empty report says so in one line — silence is never the signal.
"""

from __future__ import annotations

from pathlib import Path

from .findings import severity_rank
from .store import ReconcileResult

_REASON_NOTES = {
    "new": "",
    "returned": " (resolved earlier, now back)",
    "escalated": " (severity escalated)",
    "aging": " (still open after 3 nights)",
    "snooze-expired": " (snooze expired)",
}


def _date_of(iso_timestamp: str) -> str:
    return iso_timestamp[:10]


def _new_line(row: dict) -> str:
    note = _REASON_NOTES.get(row.get("reason", ""), "")
    return f"- **{row['severity']}** ({row['lens']}) {row['subject']}: {row['detail']}{note}"


def _open_line(row: dict) -> str:
    return (
        f"- [ ] {row['severity']} since {_date_of(row['first_seen'])} "
        f"({row['lens']}) {row['subject']}: {row['detail']}"
    )


def _resolved_line(row: dict) -> str:
    return f"- [x] ({row['lens']}) {row['subject']}: {row['detail']}"


def _coverage_line(not_run: list[str] | tuple[str, ...]) -> str:
    return (
        f"Partial coverage: lenses not run tonight: {', '.join(not_run)}. "
        "Their open items were carried forward unverified, not resolved."
    )


def render_report(
    result: ReconcileResult,
    *,
    tenant: str,
    date: str,
    advisory: str = "",
    not_run: list[str] | tuple[str, ...] = (),
) -> str:
    """``not_run`` names the lenses that crashed or were skipped this night;
    the report says so up front, because a quiet section from a lens that
    never ran is not a clean section (honesty audit 2026-09-03, 04-F2)."""
    lines = [f"# Auditor report — {tenant} — {date}", ""]
    if not_run:
        lines.extend([_coverage_line(not_run), ""])

    if not result.new and not result.open and not result.resolved:
        lines.append("All clear: nothing new, the checklist is empty, nothing changed overnight.")
        lines.append("")
        if advisory:
            lines.extend(["## Advisory", "", advisory, ""])
        return "\n".join(lines)

    lines.append("## New since the last report")
    lines.append("")
    if result.new:
        ordered = sorted(
            result.new, key=lambda r: (-severity_rank(r["severity"]), r["lens"], r["subject"])
        )
        lines.extend(_new_line(r) for r in ordered)
    else:
        lines.append("Nothing new.")
    lines.append("")

    lines.append("## Open checklist")
    lines.append("")
    if result.open:
        lines.extend(_open_line(r) for r in result.open)  # already oldest first
    else:
        lines.append("The checklist is empty.")
    lines.append("")

    lines.append("## Resolved since the last report")
    lines.append("")
    if result.resolved:
        lines.extend(_resolved_line(r) for r in result.resolved)
    else:
        lines.append("Nothing resolved.")
    lines.append("")

    if advisory:
        lines.append("## Advisory")
        lines.append("")
        lines.append(advisory)
        lines.append("")

    return "\n".join(lines)


def write_report(report_dir: str | Path, *, date: str, text: str) -> Path:
    """Write (or regenerate) the day's report. The auditor's only delivery surface."""
    target = Path(report_dir).expanduser() / f"audit-{date}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
