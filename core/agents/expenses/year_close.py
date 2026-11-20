"""The books close once a year, by calendar year (the owner, 2026-11-10:
months are never sealed). Whoever looks over every month asks for it once the
year is over; the card carries the year's numbers, as the books read them, and
a yes on it is the close. Nothing here writes money: the door computes the
numbers with the same code as the books tool, and the card only records them.

Once a year is closed nothing dated in it changes: the door refuses with one
sentence (a late receipt is offered the open year), a statement leaves the
closed year's lines out, and a closed month raises no new review card.

A closed year can be reopened (the owner, 2026-11-10). The reopen card names
who asked, so the ministry's own safeguards say who may answer it: a solo
ministry's missionary reopens alone, after Tim warns it may change last
year's books; at a church the bookkeeper asks and a supervisor says yes; at
an organization accounting asks and its supervisor says yes. The latest yes
stands: a reopen opens the year, and a close after it closes it again."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ...authority import CardRule
from ...engine.contracts import ApprovalSpec, JobContext, JobOutput
from ...engine.runkey import RunKey

CLOSE_CARD = "expenses.close_year"
REOPEN_CARD = "expenses.reopen_year"
REOPEN_RULE = CardRule("approve", "books", money=False, submitter="by")
"""Who may answer a reopen: whoever approves for the books, never the one
who asked where the ministry's safeguards say nobody approves their own."""


@dataclass(frozen=True)
class Closed:
    year: str
    on: str  # the day the yes landed, YYYY-MM-DD


def closed_years(cards: Iterable[dict]) -> dict[str, Closed]:
    """Each year closed now: the yeses on its close and reopen cards, in the
    order they came, the latest standing. A close of a year already closed
    keeps the first day; a card without its kind is a close."""
    said: list[tuple[str, int, str, str]] = []
    for n, card in enumerate(cards):
        params = card.get("params") or {}
        year = str(params.get("year") or "")
        if card.get("status") != "approved" or not re.fullmatch(r"\d{4}", year):
            continue
        kind = str(card.get("action_type") or CLOSE_CARD)
        said.append((str(card.get("resolved_at") or ""), n, kind, year))
    out: dict[str, Closed] = {}
    for at, _n, kind, year in sorted(said):
        if kind == REOPEN_CARD:
            out.pop(year, None)
        elif year not in out:
            out[year] = Closed(year, at[:10])
    return out


def closed_for(day: str, closed: dict[str, Closed]) -> Closed | None:
    """The close that holds a day (or a month), or None when its year is open."""
    return closed.get(str(day or "")[:4]) if re.match(r"\d{4}-\d{2}", str(day or "")) else None


def on_words(closed: Closed) -> str:
    try:
        on = date.fromisoformat(closed.on)
    except ValueError:
        return ""
    return f" on {on:%B} {on.day}, {on.year}"


def refusal(closed: Closed, today: date, *, late: bool = False) -> str:
    """The one sentence a change to a closed year gets."""
    said = f"The {closed.year} books are closed (the yes came{on_words(closed)}), so nothing "
    said += f"dated in {closed.year} changes now."
    if late:
        return (
            said + f" If it should still count, it goes in {today.year}'s books: ask them, and "
            f"on their yes pass day {today.isoformat()} with their words in said, so its own "
            "day stays in their words."
        )
    return said + " It stays as it was. Tell them so in those words."


def from_cards(rows: Iterable) -> dict[str, Closed]:
    """closed_years over approval_queue rows (params as JSON text)."""
    return closed_years(
        {
            "action_type": r["action_type"],
            "status": r["status"],
            "resolved_at": r["resolved_at"],
            "params": json.loads(r["params_json"] or "{}"),
        }
        for r in rows
    )


APPROVED = (
    "SELECT action_type, status, resolved_at, params_json FROM approval_queue "
    "WHERE tenant = ? AND action_type IN (?, ?) AND status = 'approved' ORDER BY id"
)
"""The close and reopen cards said yes to; its marks are marks(tenant)."""


def marks(tenant: str) -> tuple[str, str, str]:
    return (tenant, CLOSE_CARD, REOPEN_CARD)


def read(conn, tenant: str) -> dict[str, Closed]:
    """The closed years of a tenant, from its ledger."""
    return from_cards(conn.execute(APPROVED, marks(tenant)).fetchall())


def left_out(days: Iterable[str], closed: dict[str, Closed]) -> str:
    """What the person hears about lines a closed year kept out, or ""."""
    out = [d for d in days if closed_for(d, closed)]
    if not out:
        return ""
    years = " and ".join(sorted({d[:4] for d in out}))
    return (
        f" {len(out)} {'was' if len(out) == 1 else 'were'} dated in the closed {years} books "
        "and left out."
    )


# ---- the job: park the close card --------------------------------------------------

NUMBERS = ("came_in", "spent", "spent_by_kind", "owed_back", "owed_to_ministry", "waiting")


def key(ctx: JobContext) -> str:
    k = RunKey(ctx, "expcloseyear")
    for name in ("year", "by", "said", "what", "digest", *NUMBERS):
        k.param(name)
    k.value("closed", sorted(read(ctx.ledger.conn, ctx.tenant_slug)))
    year = str(ctx.params.get("year") or "")
    k.add("rejected", _rejected(ctx, year))
    reopened = _count(ctx, REOPEN_CARD, "approved", year)
    if reopened:
        k.add("reopened", reopened)
    return k.digest()


def _count(ctx: JobContext, kind: str, status: str, year: str) -> int:
    """How many cards of ``kind`` for ``year`` were ``status``."""
    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status = ?",
        (ctx.tenant_slug, kind, status),
    ).fetchall()
    return sum(1 for (p,) in rows if json.loads(p or "{}").get("year") == year)


def _rejected(ctx: JobContext, year: str) -> int:
    return _count(ctx, CLOSE_CARD, "rejected", year)


def run(ctx: JobContext) -> JobOutput:
    """The close card for a year, carrying the numbers the door read for it.
    The same numbers asked again are the same card; a card turned down is
    asked afresh."""
    year = str(ctx.params.get("year") or "")
    digest = str(ctx.params.get("digest") or "")
    if not re.fullmatch(r"\d{4}", year) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return JobOutput(status="ok", summary="expenses close_year: not a year to close")
    if year in read(ctx.ledger.conn, ctx.tenant_slug):
        return JobOutput(status="ok", summary=f"expenses close_year: {year} is already closed")
    params = {"year": year, "digest": digest, "stays": f"Nothing dated in {year} changes now"}
    for name in ("by", "said", "what", *NUMBERS):
        params[name] = str(ctx.params.get(name) or "")
    again = _rejected(ctx, year)
    reopened = _count(ctx, REOPEN_CARD, "approved", year)
    return JobOutput(
        status="ok",
        summary=f"expenses close_year: {year} waits on a yes",
        approvals=[
            ApprovalSpec(
                key=f"closeyear:{year}:{digest[:16]}"
                + (f":reask-{again}" if again else "")
                + (f":after-reopen-{reopened}" if reopened else ""),
                action_type=CLOSE_CARD,
                params=params,
                reason=f"close the {year} books: nothing dated in {year} changes after the yes",
            )
        ],
    )


# ---- the job: park the reopen card -------------------------------------------------


def warning(year: str) -> str:
    """What reopening a closed year may do, said before anyone says yes."""
    return (
        f"Reopening the {year} books may change last year's numbers, the ones the "
        f"{year} 1099s and the 990 were taken from. Anything already filed from them "
        "may then need correcting."
    )


def reopen_key(ctx: JobContext) -> str:
    k = RunKey(ctx, "expreopenyear")
    for name in ("year", "by", "said"):
        k.param(name)
    k.value("closed", sorted(read(ctx.ledger.conn, ctx.tenant_slug)))
    year = str(ctx.params.get("year") or "")
    k.add("closes", _count(ctx, CLOSE_CARD, "approved", year))
    k.add("rejected", _count(ctx, REOPEN_CARD, "rejected", year))
    return k.digest()


def reopen(ctx: JobContext) -> JobOutput:
    """The reopen card for a closed year, naming who asked (the safeguards
    keep a church or an organization from answering its own) and carrying
    the warning. The same ask again is the same card; each close gets its
    own reopen, and one turned down is asked afresh."""
    year = str(ctx.params.get("year") or "")
    if not re.fullmatch(r"\d{4}", year):
        return JobOutput(status="ok", summary="expenses reopen_year: not a year to reopen")
    if year not in read(ctx.ledger.conn, ctx.tenant_slug):
        return JobOutput(status="ok", summary=f"expenses reopen_year: {year} is not closed")
    closes = _count(ctx, CLOSE_CARD, "approved", year)
    again = _count(ctx, REOPEN_CARD, "rejected", year)
    params = {
        "year": year,
        "by": str(ctx.params.get("by") or ""),
        "said": str(ctx.params.get("said") or ""),
        "warning": warning(year),
        "what": f"reopening the {year} books",
    }
    return JobOutput(
        status="ok",
        summary=f"expenses reopen_year: {year} waits on a yes",
        approvals=[
            ApprovalSpec(
                key=f"reopenyear:{year}:{closes}" + (f":reask-{again}" if again else ""),
                action_type=REOPEN_CARD,
                params=params,
                reason=f"reopen the {year} books: {warning(year)}",
            )
        ],
    )
