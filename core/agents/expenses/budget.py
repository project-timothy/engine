"""A ministry's budget for the year, by kind of spending (board item
t-year-budget; the owner, 2026-10-15: small, micro and one-missionary
ministries first).

Someone who looks over every month says it in their own words ("our
scholarship budget this year is $12,000"); the expenses job records it as an
event, and the last thing said for a kind and year is the budget. The year
report reads it back beside what was spent: spent, left, and how much of
the year is gone, so "how much is left for scholarships?" has one answer.
Code, never a model: the sums are the month report's own dollars. Nothing
here is a tenant's: the kinds are the ministry's own categories."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

BUDGET_EVENT = "expenses.budget_set"


def latest(payloads: Iterable[dict], year: str) -> dict[str, int]:
    """Each kind's budget for ``year`` in cents: the last one said wins."""
    out: dict[str, int] = {}
    for p in payloads:
        if str(p.get("year")) == year and p.get("kind"):
            out[str(p["kind"])] = int(p.get("cents", 0))
    return out


def year_gone(year: str, today: date) -> int:
    """How much of ``year`` is behind us, in whole percent."""
    y = int(year)
    if y < today.year:
        return 100
    if y > today.year:
        return 0
    days = (date(y + 1, 1, 1) - date(y, 1, 1)).days
    return round(100 * ((today - date(y, 1, 1)).days + 1) / days)


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def standing(
    budgets: dict[str, int], spent: dict[str, int], *, year: str, today: date
) -> list[dict]:
    """Each budgeted kind: its budget, what was spent, what's left, and one
    plain sentence saying so."""
    gone = year_gone(year, today)
    out = []
    for kind, cents in sorted(budgets.items()):
        used = spent.get(kind, 0)
        left = cents - used
        share = round(100 * used / cents) if cents else 0
        says = f"{kind}: {dollars(used)} spent of {dollars(cents)}, "
        says += f"{dollars(left)} left" if left >= 0 else f"{dollars(-left)} over"
        if 0 < gone < 100:
            says += f" ({share}% of the budget used, {gone}% of the year gone)"
        out.append(
            {
                "kind": kind,
                "budget": f"{cents / 100:.2f}",
                "spent": f"{used / 100:.2f}",
                "left": f"{left / 100:.2f}",
                "says": says + ".",
            }
        )
    return out
