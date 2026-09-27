"""Amount+date receipt dedup.

The 2026-05-18 lesson: the same paper receipt scanned twice has different
bytes, so file-hash dedup misses it. Two receipts with the same amount whose
dates fall within a small window are one purchase until a human says
otherwise. This module only GROUPS suspects; nothing is dropped or merged —
the report card carries the flag and the owner decides (invariant 2 spirit:
code proposes, a human disposes of money questions).
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def duplicate_groups(incoming: list[dict], day_window: int = 2) -> list[set[str]]:
    """Groups of filenames that look like one purchase scanned twice.

    Two items group when their amounts are equal (both present) and their
    dates are at most ``day_window`` days apart (both present). Grouping is
    transitive within an amount. Items missing an amount or date never
    group — absence of data is never evidence of duplication.
    """
    by_amount: dict[Any, list[tuple[date, str]]] = {}
    for item in incoming:
        amount = item.get("amount")
        when = _to_date(item.get("date"))
        name = item.get("file")
        if amount is None or when is None or not name:
            continue
        by_amount.setdefault(amount, []).append((when, str(name)))

    groups: list[set[str]] = []
    for entries in by_amount.values():
        entries.sort()
        current: set[str] = set()
        last: date | None = None
        for when, name in entries:
            if last is not None and (when - last).days <= day_window:
                current.add(name)
            else:
                if len(current) > 1:
                    groups.append(current)
                current = {name}
            last = when
        if len(current) > 1:
            groups.append(current)
    return groups
