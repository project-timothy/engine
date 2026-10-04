"""CLOSE_PREFLIGHT.md: the legacy close script's surface, kept on purpose.

Same name, same place (``<report_dir>/<month>/CLOSE_PREFLIGHT.md``), same
exit-code contract (0 all OK, 1 WARN present, 2 BLOCK present). Checks in
canonical order with one evidence line each; unresolved items collect into
a to-do list because the preflight's whole job is telling the owner what
stands between now and a sealed month.
"""

from __future__ import annotations

from pathlib import Path

from ...engine.guard import WriteGuard
from .schema import CHECK_ORDER, CheckResult, PreflightReport

_STATUS_NOTES = {
    "OK": "",
    "WARN": "resolve before sign-off",
    "BLOCK": "the close cannot proceed past this",
    "TODO": "not yet built; not counted",
}


def _ordered(checks: list[CheckResult]) -> list[CheckResult]:
    rank = {name: i for i, name in enumerate(CHECK_ORDER)}
    return sorted(checks, key=lambda c: rank.get(c.name, len(rank)))


def render_preflight(report: PreflightReport) -> str:
    lines = [
        f"# Close preflight — {report.tenant} — {report.month}",
        "",
        f"Run {report.ran_at[:16].replace('T', ' ')} · verdict **{report.worst}** "
        f"(exit {report.exit_code})",
        "",
    ]
    for i, check in enumerate(_ordered(report.checks), 1):
        note = _STATUS_NOTES[check.status]
        lines.append(
            f"{i}. **{check.status}** `{check.name}` — {check.summary}"
            + (f" ({note})" if note and check.status != "OK" else "")
        )
    lines.append("")

    todo = [c for c in _ordered(report.checks) if c.status in ("WARN", "BLOCK")]
    unbuilt = sum(1 for c in report.checks if c.status == "TODO")
    lines.append("## To resolve")
    lines.append("")
    if not todo and unbuilt:
        lines.append(
            f"Nothing outstanding from the built checks ({unbuilt} check(s) not yet built "
            "still hold TODO slots above)."
        )
    elif not todo:
        lines.append("Nothing. The month is ready for the packet and sign-off.")
    for check in todo:
        if check.details:
            for detail in check.details:
                lines.append(f"- [ ] ({check.name}) {detail}")
        else:
            lines.append(f"- [ ] ({check.name}) {check.summary}")
    lines.append("")
    return "\n".join(lines)


def write_preflight(report: PreflightReport, *, report_dir: str, guard: WriteGuard) -> Path:
    target = Path(report_dir).expanduser() / report.month / "CLOSE_PREFLIGHT.md"
    checked = guard.check_write(target)
    checked.parent.mkdir(parents=True, exist_ok=True)
    checked.write_text(render_preflight(report), encoding="utf-8")
    return checked
