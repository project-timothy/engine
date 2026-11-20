"""Severity clocks: when each open security finding is due (chunk 4).

Every open issue labeled ``security`` carries one ``sev:`` label. Its clock
starts the day that label went on and runs the window
``docs/security/charter.md`` names (a test holds the two together). This is
a report, never a gate: it reads GitHub through ``gh``.

    uv run python -m core.evals.security_clocks          # one line per finding
    uv run python -m core.evals.security_clocks --json   # for the build board
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

WINDOWS = {"sev:critical": 7, "sev:high": 30, "sev:medium": 90, "sev:low": 180}
"""Days from the severity label to due; the charter's table."""


@dataclass(frozen=True)
class Clock:
    number: int
    title: str
    severity: str | None
    started: date | None
    due: date | None
    days_left: int | None

    @property
    def overdue(self) -> bool:
        return self.days_left is not None and self.days_left < 0


def _day(stamp: str) -> date:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()


def clocks(issues: list[dict], today: date) -> list[Clock]:
    """One clock per issue: ``number``, ``title``, ``labels`` (names) and
    ``labeled`` (each sev label's ISO time it went on)."""
    out = []
    for issue in issues:
        sevs = [name for name in issue["labels"] if name in WINDOWS]
        if not sevs:
            out.append(Clock(issue["number"], issue["title"], None, None, None, None))
            continue
        severity = min(sevs, key=WINDOWS.__getitem__)
        started = _day(issue["labeled"][severity])
        due = started + timedelta(days=WINDOWS[severity])
        days_left = (due - today).days
        out.append(Clock(issue["number"], issue["title"], severity, started, due, days_left))
    return out


def summary(found: list[Clock]) -> dict[str, int]:
    return {
        "open": len(found),
        "overdue": sum(c.overdue for c in found),
        "unrated": sum(c.severity is None for c in found),
    }


def render(found: list[Clock]) -> list[str]:
    """Most urgent first: overdue, then unrated, then by days left."""

    def urgency(c: Clock) -> tuple[int, int]:
        if c.days_left is None:
            return (1, 0)
        return (0 if c.overdue else 2, c.days_left)

    lines = []
    for c in sorted(found, key=urgency):
        if c.days_left is None:
            lines.append(f"#{c.number} no severity: {c.title}")
        elif c.overdue:
            lines.append(
                f"#{c.number} {c.severity} overdue {-c.days_left}d (due {c.due}): {c.title}"
            )
        else:
            lines.append(f"#{c.number} {c.severity} due {c.due} ({c.days_left}d): {c.title}")
    return lines


def labeled_at(events: list[dict]) -> dict[str, str]:
    """When each sev label last went on, from an issue's timeline."""
    found: dict[str, str] = {}
    for ev in events:
        name = (ev.get("label") or {}).get("name", "")
        if ev.get("event") == "labeled" and name in WINDOWS:
            found[name] = max(found.get(name, ""), ev["created_at"])
    return found


def _gh(*args: str) -> object:
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def fetch() -> list[dict]:
    """The open security issues of the repository ``gh`` is pointed at."""
    raw = _gh(
        "issue", "list", "--label", "security", "--state", "open",
        "--limit", "200", "--json", "number,title,labels",
    )  # fmt: skip
    issues = []
    for item in raw if isinstance(raw, list) else []:
        events = _gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/issues/{item['number']}/timeline",
        )
        flat = [ev for page in events for ev in page] if isinstance(events, list) else []
        issues.append(
            {
                "number": item["number"],
                "title": item["title"],
                "labels": [label["name"] for label in item["labels"]],
                "labeled": labeled_at(flat),
            }
        )
    return issues


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    found = clocks(fetch(), datetime.now(UTC).date())
    counts = summary(found)
    if "--json" in argv:
        rows = [
            {
                "number": c.number,
                "title": c.title,
                "severity": c.severity,
                "due": c.due.isoformat() if c.due else None,
                "days_left": c.days_left,
            }
            for c in found
        ]
        print(json.dumps(counts | {"findings": rows}, indent=2))
        return 0
    for line in render(found):
        print(line)
    print(f"open {counts['open']}, overdue {counts['overdue']}, no severity {counts['unrated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
