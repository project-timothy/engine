"""A ministry's statements from its own books (board chunk c29, t-board-pack;
the owner, 2026-11-12: the board gets its statements every month without
anyone building them by hand).

Read from the double-entry journal (journal.py), by code, no model:

- The statement of activities: what came in and what was spent, by kind,
  for a span, and the change in net assets.
- The statement of financial position at the span's end: money in the bank,
  cash in hand, ministry money people hold or owe it, what the ministry owes
  people, and net assets. Net assets must equal the opening balance plus
  everything that came in less everything spent since: the journal proves
  it, and ``Position.ties`` says so.
- The functional split of the span's spending: program and management, by
  the share the ministry set (``books.program_share``, newest wins, said by
  whoever looks over every month). Each kind splits to the cent.

Pure functions, plus the one small job that keeps the share.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ...engine.contracts import EventSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from . import journal as j
from .common import _events_of
from .statements import shares

SPLIT_EVENT = "books.program_share"  # the share of spending that is program, newest wins


# ---- the statements ----------------------------------------------------------------


@dataclass(frozen=True)
class Activities:
    income: dict[str, int]
    expenses: dict[str, int]

    @property
    def total_income(self) -> int:
        return sum(self.income.values())

    @property
    def total_expenses(self) -> int:
        return sum(self.expenses.values())

    @property
    def change(self) -> int:
        return self.total_income - self.total_expenses


def activities(span: dict[str, int]) -> Activities:
    """The statement of activities from the span's own entries' totals."""
    return Activities(j.by_kind(span, j.INCOME), j.by_kind(span, j.EXPENSES))


@dataclass(frozen=True)
class Position:
    in_the_bank: dict[str, int]
    cash_in_hand: dict[str, int]
    held_by_people: dict[str, int]
    owed_to_people: dict[str, int]
    opening: int
    ever_in: int
    ever_spent: int
    other: dict[str, int] = field(default_factory=dict)
    with_payment_services: dict[str, int] = field(default_factory=dict)  # c32

    @property
    def total_assets(self) -> int:
        return (
            sum(self.in_the_bank.values())
            + sum(self.cash_in_hand.values())
            + sum(self.with_payment_services.values())
            + sum(self.held_by_people.values())
        )

    @property
    def total_liabilities(self) -> int:
        return sum(self.owed_to_people.values())

    @property
    def net_assets(self) -> int:
        return self.total_assets - self.total_liabilities

    @property
    def ties(self) -> bool:
        """Net assets are the opening plus all that came in less all spent."""
        return not self.other and self.net_assets == self.opening + self.ever_in - self.ever_spent


def position(at_end: dict[str, int]) -> Position:
    """The statement of financial position from every entry through the end."""

    def under(root: str) -> dict[str, int]:
        return {a.split(":", 2)[2]: c for a, c in at_end.items() if a.startswith(root + ":") and c}

    to, by = j.owed(at_end)
    known = (j.BANK, j.CASH, j.PROCESSOR, j.PEOPLE, j.INCOME, j.EXPENSES, j.OPENING)
    services = {
        "{} ({})".format(*k.split(":", 1)): c for k, c in under(j.PROCESSOR).items()
    }  # "PayPal (Ruth Okafor)": what each service holds for the ministry
    return Position(
        in_the_bank=under(j.BANK),
        cash_in_hand=under(j.CASH),
        with_payment_services=services,
        held_by_people=by,
        owed_to_people=to,
        opening=-at_end.get(j.OPENING, 0),
        ever_in=sum(j.by_kind(at_end, j.INCOME).values()),
        ever_spent=sum(j.by_kind(at_end, j.EXPENSES).values()),
        other={a: c for a, c in at_end.items() if c and not a.startswith(known)},
    )


@dataclass(frozen=True)
class Functional:
    program_share: int
    by_kind: dict[str, tuple[int, int]]  # kind -> (program, management)

    @property
    def program(self) -> int:
        return sum(p for p, _m in self.by_kind.values())

    @property
    def management(self) -> int:
        return sum(m for _p, m in self.by_kind.values())


def functional(expenses: dict[str, int], program_share: int) -> Functional:
    """Each kind of spending split program and management, to the cent."""
    split = {}
    for kind, cents in sorted(expenses.items()):
        program, management = shares(cents, [program_share, 100 - program_share])
        split[kind] = (program, management)
    return Functional(program_share, split)


# ---- the share, as said ------------------------------------------------------------


def latest_share(events: Iterable[dict]) -> int | None:
    """The share of spending that is program, as last said; None if never."""
    share = None
    for e in events:
        share = int(e["percent"])
    return share


def split_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expprogramshare")
    for name in ("percent", "by", "said"):
        key.param(name)
    key.add("before", _count(ctx))
    return key.digest()


def _count(ctx: JobContext) -> int:
    return len(_events_of(ctx, SPLIT_EVENT))


def split_run(ctx: JobContext) -> JobOutput:
    """The share of the ministry's spending that is program (the rest is
    management), as someone who looks over every month said it."""
    percent = str(ctx.params.get("percent") or "").strip()
    if not re.fullmatch(r"\d{1,3}", percent) or int(percent) > 100:
        return JobOutput(status="ok", summary="expenses program share: not a share")
    payload = {
        "percent": int(percent),
        "by": str(ctx.params.get("by") or ""),
        "said": str(ctx.params.get("said") or ""),
    }
    return JobOutput(
        status="ok",
        summary=f"expenses program share: {percent}% program",
        events=[
            EventSpec(key=f"program-share:{_count(ctx)}", event_type=SPLIT_EVENT, payload=payload)
        ],
    )
