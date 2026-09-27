#!/usr/bin/env python3
"""Turn a phase plan's markdown tables into GitHub issues, idempotently.

Usage: uv run python scripts/lane/issues_from_plan.py docs/phase-7-plan.md [--phase 7] [--dry-run]

Every table whose header starts with ``| Id | Title |`` is a set of rows.
Each row becomes one issue titled ``[<id>] <title>`` under an epic issue
titled ``Phase <n>: <plan title>``; an existing issue with the same title is
left alone. Labels: ``phase-<n>``, ``size-<S|M|L>``, ``one-way-door`` when the
Door column says so, ``ready`` when Depends is ``none``, else ``blocked``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FIELDS = ("Id", "Title", "Goal", "Acceptance", "Touches", "Size", "Depends", "Door")


@dataclass(frozen=True)
class Row:
    id: str
    title: str
    goal: str
    acceptance: str
    touches: str
    size: str
    depends: str
    door: str

    @property
    def issue_title(self) -> str:
        return f"[{self.id}] {self.title}"

    @property
    def labels(self) -> list[str]:
        phase = self.id.split(".")[0]
        labels = [f"phase-{phase}"]
        size = self.size.strip()[:1].upper()
        if size in "SML":
            labels.append(f"size-{size}")
        if self.door.strip().lower().startswith("one-way"):
            labels.append("one-way-door")
        labels.append("ready" if self.depends.strip().lower() == "none" else "blocked")
        return labels

    def body(self, epic: int | None) -> str:
        head = f"Part of #{epic}\n\n" if epic else ""
        return (
            f"{head}## Goal\n\n{self.goal}\n\n## Acceptance\n\n{self.acceptance}\n\n"
            f"## Touches\n\n{self.touches}\n\n## Size\n\n{self.size}\n\n"
            f"## Depends\n\n{self.depends}\n\n## Door\n\n{self.door}\n"
        )


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_plan(text: str) -> tuple[str, list[Row]]:
    """Return (plan title, rows) from the plan's markdown."""
    title_match = re.search(r"^# (.+)$", text, re.M)
    title = title_match.group(1).strip() if title_match else "Phase plan"
    rows: list[Row] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and _cells(lines[i])[:1] == ["Id"]:
            header = _cells(lines[i])
            if tuple(header) != FIELDS:
                raise ValueError(f"table header {header} is not the six-field shape {FIELDS}")
            i += 2  # skip the separator row
            while i < len(lines) and lines[i].startswith("|"):
                cells = _cells(lines[i])
                if len(cells) != len(FIELDS):
                    raise ValueError(
                        f"row has {len(cells)} cells, expected {len(FIELDS)}: {lines[i][:60]}"
                    )
                rows.append(Row(*cells))
                i += 1
            continue
        i += 1
    ids = [r.id for r in rows]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        raise ValueError(f"duplicate row ids: {sorted(dupes)}")
    return title, rows


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def existing_titles() -> dict[str, int]:
    out = _gh("issue", "list", "--state", "all", "--limit", "500", "--json", "title,number")
    return {item["title"]: item["number"] for item in json.loads(out)}


def ensure_issue(title: str, body: str, labels: list[str], known: dict[str, int], dry: bool) -> int:
    if title in known:
        return known[title]
    if dry:
        print(f"would create: {title} {labels}")
        return 0
    args = ["issue", "create", "--title", title, "--body", body]
    for label in labels:
        args += ["--label", label]
    url = _gh(*args).strip()
    number = int(url.rsplit("/", 1)[-1])
    known[title] = number
    print(f"created #{number}: {title}")
    return number


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", type=Path)
    ap.add_argument("--phase", default=None, help="epic phase number (default: from row ids)")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    title, rows = parse_plan(ns.plan.read_text())
    phase = ns.phase or rows[0].id.split(".")[0]
    known = {} if ns.dry_run else existing_titles()
    epic_title = f"Phase {phase}: {title}"
    epic_body = (
        "Epic for `"
        + str(ns.plan)
        + "`. Rows:\n\n"
        + "\n".join(f"- [{r.id}] {r.title} ({r.size.strip()[:1]})" for r in rows)
        + "\n"
    )
    epic = ensure_issue(epic_title, epic_body, [f"phase-{phase}"], known, ns.dry_run)
    for r in rows:
        ensure_issue(r.issue_title, r.body(epic or None), r.labels, known, ns.dry_run)
    print(f"{len(rows)} rows under epic #{epic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
