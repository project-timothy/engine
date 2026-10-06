"""Append-only JSONL event log.

The SQLite ``events`` table is the queryable mirror; this file is the
human-diffable, git-friendly record. One JSON object per line. Writers only
append: idempotency is enforced upstream (the SQLite unique key decides
whether a line is new), so this writer stays dumb on purpose.

The one rewrite is ``repair_event_log``, under the run lock, after a crash
(#136): it truncates the file at its first line that does not parse (a torn
tail, or real corruption) and re-appends SQLite's rows from there.
``event_log_is_whole`` is the cheap check that lets a healthy log skip it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EVENT_LOG_FILENAME = "event_log.jsonl"


def append_event_line(root: Path, record: dict[str, Any]) -> None:
    """Append one event record as a single JSON line under ``root``."""
    path = root / EVENT_LOG_FILENAME
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_event_lines(root: Path) -> list[dict[str, Any]]:
    """Read back every event record. Returns [] if the log does not exist.

    A torn FINAL line (a crash mid-append, #136) is skipped: SQLite holds
    the authoritative copy and the runner repairs the file on its next
    locked open. A malformed line anywhere else is real corruption and
    still raises.
    """
    path = root / EVENT_LOG_FILENAME
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if ln]
    records: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break  # torn tail: tolerated, repaired at the next locked open
            raise
    return records


def event_log_is_whole(root: Path, row_count: int) -> bool:
    """True when every line parses and there are at least ``row_count`` of
    them: exactly the state in which ``repair_event_log`` would change
    nothing, so the caller can skip loading SQLite's rows (#14)."""
    path = root / EVENT_LOG_FILENAME
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False
    return len(lines) >= row_count


def repair_event_log(root: Path, rows: list[dict[str, Any]]) -> None:
    """Converge the JSONL file onto ``rows`` (SQLite's authoritative events).

    Two crash shapes (#136): a torn trailing fragment (append died mid-
    write) is truncated away; missing tail lines (commit landed, append
    never ran) are re-appended from SQLite. A line that does not parse
    anywhere else is truncated the same way, and SQLite's rows are
    re-appended from that point. Call only under the run lock.
    """
    path = root / EVENT_LOG_FILENAME
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    valid = 0
    for line in lines:
        try:
            json.loads(line)
            valid += 1
        except json.JSONDecodeError:
            break
    if valid < len(lines):
        with path.open("w", encoding="utf-8") as handle:
            for line in lines[:valid]:
                handle.write(line + "\n")
    for record in rows[valid:]:
        append_event_line(root, record)
