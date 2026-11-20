"""The year's close and budget: closing and reopening a year, the refusal
of a dated write into a closed year, the budget, and the program share.
Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.expenses import records, year_close
from ..agents.expenses.categories import canonical, from_words
from ..agents.expenses.records import DATED_EVENT, LANDED_EVENT, REVIEW_CARD
from ..authority import card_request, evaluate
from .books import _books
from .reads import _events, _log, _sees_every_month
from .rules import _pick_kind
from .spec import ToolSpec, _schema

if TYPE_CHECKING:
    from .catalog import Tools


def _set_program_share(t: Tools, args: dict) -> dict:
    """What share of the ministry's spending is program, the rest management
    (c29), in the words of whoever looks over every month. Newest wins."""
    from ..engine.runner import door_run as run

    if not _sees_every_month(t):
        return {
            "status": "refused",
            "say": "That's set by whoever looks over every month at your ministry.",
        }
    raw = str(args.get("percent") or "").replace("%", "").strip()
    if not raw.isdigit() or int(raw) > 100:
        return {"status": "needs", "say": "Say the program share as a whole percent, like 95."}
    done = run(
        t.tenant,
        "expenses",
        "program_share",
        params={
            "percent": raw,
            "by": t.viewer.person if t.viewer is not None else "",
            "said": str(args.get("said") or ""),
        },
        ledger_dir=Path(t.receipts.ledger_root).parent,
        tenants_root=t.receipts.tenants_root,
    )
    if done.status == "error":
        return {"status": "busy", "say": "Tim couldn't keep that just now. Try again soon."}
    return {
        "status": "set",
        "say": f"Done: {raw}% of the ministry's spending counts as program and "
        f"{100 - int(raw)}% as management, from now on in its statements.",
    }


# ---- the year's close (the owner, 2026-11-10: the books close once a year) --------

_DATED_WRITES = frozenset(
    {
        "paid_it_myself",
        "set_category",
        "set_drive_purpose",
        "set_amount",
        "set_receipt_day",
        "record_reading",
        "agree_to_readings",
        "send_month_for_review",
        "explain_bank_line",
        "split_line",
        "pick_card_receipt",
    }
)
"""The writes that name something dated: a line, a day, or a month."""


def _closed(t: Tools) -> dict[str, year_close.Closed]:
    return year_close.from_cards(t._rows(year_close.APPROVED, year_close.marks(t.tenant)))


def _day_of_line(t: Tools, line: str) -> str:
    """The day the books hold a line on: a receipt's own day, a drive's, a
    statement line's, or a cash or school payment's; "" when unknown."""
    found = re.fullmatch(r"events#(\d+)", line)
    if found:
        rows = t._rows(
            "SELECT event_type, payload_json FROM events WHERE id = ? AND tenant = ?",
            (int(found[1]), t.tenant),
        )
        if not rows:
            return ""
        payload = json.loads(rows[0]["payload_json"] or "{}")
        if rows[0]["event_type"] == records.DRIVE_EVENT:
            return str(payload.get("start", ""))[:10]
        if rows[0]["event_type"] == LANDED_EVENT:
            sha = str(payload.get("sha256", ""))
            dated = [
                str(e.get("day")) for _i, e in _events(t, DATED_EVENT) if e.get("sha256") == sha
            ]
            return dated[-1] if dated else str(payload.get("file", ""))[:10]
        return str(payload.get("day", ""))[:10]
    found = re.fullmatch(r"bank#([0-9a-f]{16})", line)
    if found:
        for e in _log(t):
            p = e["payload"]
            if str(p.get("sha256", "")).startswith(found[1]) and p.get("day"):
                return str(p["day"])[:10]
    return ""


def _closed_refusal(t: Tools, name: str, args: dict) -> dict | None:
    """A write to something dated in a closed year, refused in one sentence;
    a late receipt's day in a closed year is offered the open year. None
    for anything else, and for every ministry that never closed a year."""
    if name not in _DATED_WRITES:
        return None
    closed = _closed(t)
    if not closed:
        return None
    today = t._today()
    held = year_close.closed_for(str(args.get("month") or ""), closed) or year_close.closed_for(
        _day_of_line(t, str(args.get("line") or "").strip()), closed
    )
    if held is not None:
        return {"status": "year_closed", "say": year_close.refusal(held, today)}
    held = year_close.closed_for(str(args.get("day") or ""), closed)
    if held is not None:
        return {"status": "year_closed", "say": year_close.refusal(held, today, late=True)}
    return None


_CLOSE_NUMBERS = (
    "came_in",
    "spent",
    "spent_by_kind",
    "owed_back_to_people",
    "owed_to_the_ministry",
)


def _waiting_months(t: Tools, year: str) -> list[str]:
    """The months of a year whose review card still waits on a yes."""
    rows = t._rows(
        "SELECT params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status = 'pending'",
        (t.tenant, REVIEW_CARD),
    )
    months = {str(json.loads(r["params_json"] or "{}").get("month", "")) for r in rows}
    return sorted(m for m in months if m.startswith(f"{year}-"))


def _close_year(t: Tools, args: dict) -> dict:
    """The year's close, asked by whoever looks over every month once the
    year is over: a card carrying the year's numbers as the books read them
    (the ones the 1099s and the 990 are taken from) and the months still
    waiting on a yes, which it shows and never refuses for. Their yes on it,
    by Face ID or the emailed link, closes the year."""
    from ..engine.runner import door_run as run

    if not _sees_every_month(t):
        return {
            "status": "refused",
            "say": "The books are closed by whoever looks over every month at your ministry.",
        }
    today = t._today()
    year = str(args.get("year") or today.year - 1).strip()
    if not (len(year) == 4 and year.isascii() and year.isdigit()):
        return {"status": "needs", "say": "Say which year, as four digits, like 2026."}
    if int(year) >= today.year:
        return {
            "status": "not_yet",
            "say": f"A year closes once it's over: {year} can close from January 1, "
            f"{int(year) + 1}. Tell them so.",
        }
    done_before = _closed(t).get(year)
    if done_before is not None:
        return {
            "status": "closed",
            "say": f"The {year} books are already closed (the yes came"
            f"{year_close.on_words(done_before)}). Tell them so.",
        }
    books = _books(t, {"period": year})
    numbers = {k: books[k] for k in _CLOSE_NUMBERS}
    digest = hashlib.sha256(json.dumps(numbers, sort_keys=True).encode()).hexdigest()
    kinds = ", ".join(f"{k} ${Decimal(v):,.2f}" for k, v in books["spent_by_kind"].items())
    what = (
        f"closing the {year} books: ${Decimal(books['came_in']):,.2f} came in and "
        f"${Decimal(books['spent']):,.2f} was spent" + (f" ({kinds})" if kinds else "")
    )
    waiting = _waiting_months(t, year)
    if waiting:
        names = [f"{date.fromisoformat(m + '-01'):%B}" for m in waiting]
        what += f"; {_and(names)} still wait on a yes"
    done = run(
        t.tenant,
        "expenses",
        "close_year",
        params={
            "year": year,
            "by": t.viewer.person if t.viewer is not None else "",
            "said": str(args.get("said") or ""),
            "what": what,
            "digest": digest,
            "came_in": books["came_in"],
            "spent": books["spent"],
            "spent_by_kind": json.dumps(books["spent_by_kind"], sort_keys=True),
            "owed_back": books["owed_back_to_people"],
            "owed_to_ministry": books["owed_to_the_ministry"],
            "waiting": str(books["still_waiting"]),
        },
        ledger_dir=Path(t.ledger_root).parent,
        tenants_root=t.receipts.tenants_root if t.receipts is not None else None,
    )
    card = next(
        (
            r
            for r in t._rows(
                "SELECT id, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
                "AND status = 'pending' ORDER BY id DESC",
                (t.tenant, year_close.CLOSE_CARD),
            )
            if json.loads(r["params_json"] or "{}").get("digest") == digest
        ),
        None,
    )
    if done.status == "error" or card is None:
        return {
            "status": "busy",
            "say": "Tim couldn't ask for that just now. Try again in a minute.",
        }
    return {
        "status": "waiting",
        "card": card["id"],
        "what": what,
        "say": f"Read them what the card says ({what}). Nothing dated in {year} changes once "
        f"it's closed. When they say yes in their own words, call decide_card with card "
        f"{card['id']} and those words; it takes their Face ID or the emailed link.",
    }


def _reopen_year(t: Tools, args: dict) -> dict:
    """A closed year reopened (the owner, 2026-11-10), asked by whoever
    looks over every month. The card names who asked, so the ministry's
    safeguards say who may answer: a solo missionary their own, a church or
    an organization someone else who approves for the books. The reply
    carries the warning, said before anyone says yes."""
    from ..engine.runner import door_run as run

    if not _sees_every_month(t):
        return {
            "status": "refused",
            "say": "A closed year is reopened by whoever looks over every month at your ministry.",
        }
    year = str(args.get("year") or "").strip()
    if not (len(year) == 4 and year.isascii() and year.isdigit()):
        return {"status": "needs", "say": "Say which year, as four digits, like 2026."}
    if year not in _closed(t):
        return {
            "status": "open",
            "say": f"The {year} books aren't closed, so there's nothing to reopen. Tell them so.",
        }
    by = t.viewer.person if t.viewer is not None else ""
    done = run(
        t.tenant,
        "expenses",
        "reopen_year",
        params={"year": year, "by": by, "said": str(args.get("said") or "")},
        ledger_dir=Path(t.ledger_root).parent,
        tenants_root=t.receipts.tenants_root if t.receipts is not None else None,
    )
    card = next(
        iter(
            t._rows(
                "SELECT id FROM approval_queue WHERE tenant = ? AND action_type = ? "
                "AND status = 'pending' ORDER BY id DESC",
                (t.tenant, year_close.REOPEN_CARD),
            )
        ),
        None,
    )
    if done.status == "error" or card is None:
        return {
            "status": "busy",
            "say": "Tim couldn't ask for that just now. Try again in a minute.",
        }
    return {
        "status": "waiting",
        "card": card["id"],
        "say": f"First tell them, in these words: {year_close.warning(year)} Then: "
        + _who_reopens(t, by, year, card["id"]),
    }


def _who_reopens(t: Tools, by: str, year: str, card: int) -> str:
    """Who says the yes on a reopen, in the words Tim passes on."""
    policy = t.viewer.policy if t.viewer is not None else None
    own = (
        policy is None
        or evaluate(
            policy, card_request(year_close.REOPEN_RULE, by, {"year": year, "by": by}, policy)
        ).allowed
    )
    if own:
        return (
            f"when they say yes in their own words, call decide_card with card {card} and "
            "those words; it takes their Face ID or the emailed link."
        )
    return (
        f"the reopen now waits on a yes from someone else who approves for the ministry's "
        f"books, never the one who asked (card {card}). Tell them so."
    )


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _set_budget(t: Tools, args: dict) -> dict:
    """The year's budget for one kind of spending, in the person's words."""
    from ..engine.config import load_tenant
    from ..engine.runner import door_run as run

    if not _sees_every_month(t):
        return {
            "status": "refused",
            "say": "The budget is set by whoever looks over every month at your ministry.",
        }
    listed = list(load_tenant(t.tenant).expenses.category_accounts)
    kind = canonical(str(args.get("kind") or ""), listed) or from_words(
        str(args.get("kind") or ""), listed
    )
    if not kind:
        return {"status": "needs", "say": "", "pick_kind": _pick_kind(listed)}
    raw = str(args.get("amount") or "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(raw)
    except ArithmeticError:
        amount = Decimal(-1)
    if amount < 0:
        return {"status": "needs", "say": "Say the year's amount in dollars."}
    year = str(args.get("year") or t._today().year).strip()
    if not (len(year) == 4 and year.isascii() and year.isdigit()):
        return {"status": "needs", "say": "Say which year, as four digits, like 2026."}
    done = run(
        t.tenant,
        "expenses",
        "budget",
        params={
            "year": year,
            "kind": kind,
            "amount": f"{amount:.2f}",
            "by": t.viewer.person if t.viewer is not None else "",
            "said": str(args.get("said") or ""),
        },
        ledger_dir=Path(t.receipts.ledger_root).parent,
        tenants_root=t.receipts.tenants_root,
    )
    if done.status == "error":
        return {"status": "busy", "say": "Tim couldn't save that just now. Try again in a minute."}
    return {
        "status": "set",
        "say": f"Your {year} budget for {kind} is ${amount:,.2f}. "
        "Ask where your year stands any time to see what's spent and what's left.",
    }


SET_BUDGET = ToolSpec(
    "set_budget",
    "Set the ministry's budget for one kind of spending for a year, when the person says it "
    'in their own words ("our scholarship budget this year is $12,000"). Pass the kind '
    "(pick the closest of the ministry's categories yourself), the year's amount, the year "
    "when not this one, and their words in said. Saying it again replaces it. Only someone "
    "who looks over every month may set it; it holds and moves no money.",
    _schema(
        {
            "kind": {"type": "string", "description": "The closest of the ministry's categories."},
            "amount": {"type": "string", "description": "The year's amount, in dollars."},
            "year": {"type": "string", "description": "The year, when not this one."},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["kind", "amount", "said"],
    ),
    _set_budget,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        'Set the budget for one kind of spending for a year, when the person says it ("our '
        "scholarship budget this year is $12,000\"): kind (pick the closest of the ministry's "
        "categories yourself), the year's amount, year when not this one, and their words in "
        "said. Saying it again replaces it."
    ),
)
"""The year's budget (t-year-budget): on the box's door, for whoever looks
over every month."""


SET_PROGRAM_SHARE = ToolSpec(
    "set_program_share",
    "Set what share of the ministry's spending is program (its mission), the rest being "
    'management, as a whole percent from the person\'s own words ("95 percent program"). '
    "The statements split every kind of spending by it. Only someone who looks over every "
    "month may set it; the newest one said stands. It moves no money.",
    _schema(
        {
            "percent": {"type": "string", "description": "The program share, like 95."},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["percent", "said"],
    ),
    _set_program_share,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Set what share of the ministry's spending is program (its mission), the rest being "
        'management: a whole percent from the person\'s own words ("95 percent program"). The '
        "newest one said stands."
    ),
)
"""The functional split (c29): on the box's door, for whoever looks over every month."""


CLOSE_YEAR = ToolSpec(
    "close_year",
    "Close the ministry's books for a year that is over, when the person says it in their "
    'own words ("close the 2026 books"). Leave year out for last year. The reply is the '
    "year's numbers and a card: read them what it says, and only on their yes call "
    "decide_card for that card with their words. Once a year is closed, nothing dated in "
    "it changes. Only someone who looks over every month may ask; it moves no money.",
    _schema(
        {
            "year": {"type": "string", "description": "The year, when not last year."},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["said"],
    ),
    _close_year,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Close the books for a year that is over, when the person says it in their own words "
        "(said; year left out for last year). The reply is the year's numbers and a card: read "
        "them what it says, and only on their yes call decide_card for that card with their "
        "words. Once a year is closed, nothing dated in it changes."
    ),
)
"""The year's close (the owner, 2026-11-10): on the box's door, for whoever
looks over every month."""


REOPEN_YEAR = ToolSpec(
    "reopen_year",
    "Reopen a year whose books were closed, when the person says it in their own words "
    '("reopen the 2026 books"). The reply starts with a warning to tell them first: '
    "reopening may change the numbers last year's 1099s and 990 were taken from. Then it "
    "says who gives the yes: at a solo ministry the person themselves, by decide_card on "
    "their yes; at a church or an organization someone else who approves for the books. "
    "Only someone who looks over every month may ask; it moves no money.",
    _schema(
        {
            "year": {"type": "string", "description": "The closed year, four digits."},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["year", "said"],
    ),
    _reopen_year,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Reopen a year whose books were closed, when the person says it in their own words "
        "(said). Tell them the reply's warning first: reopening may change the numbers last "
        "year's 1099s and 990 were taken from. Then say who gives the yes, as the reply says."
    ),
)
"""Reopening a closed year (the owner, 2026-11-10): the right people for
each size of ministry, by its own safeguards."""
