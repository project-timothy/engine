"""Donors and their letters (board chunk c32, t-donor-letters; the owner,
2026-11-12: every donor gets the letter and year-end statement the IRS
expects, without anyone writing them by hand).

A gift is a line of money in on the ministry's own account that is its
income (``statements.CAME_IN``) with the donor named: said when the line is
explained (``gift_from``), or read from a kind the ministry named for a
donor ("Gifts from <name>"), so books kept before this chunk still make
their letters. Money in that was a person's own (c31) is never a gift to
the ministry.

The letters, as ``paper`` blocks, in the formal register:

- an acknowledgment of each gift of $250 or more: a single gift that size
  needs one in writing before the donor can deduct it (the amount, the day,
  that no goods or services were given in return, and that the ministry
  keeps control of the money);
- a year-end giving statement per donor, every gift that year;
- a thank-you for a grant from a donor-advised fund, and nothing more: the
  fund's sponsor gave the donor their receipt when the donor gave to it.

What a letter needs that the books don't hold (the ministry's EIN, its
mailing address, who signs and their title, a donor's mailing address) is
said once (``DETAIL_EVENT``) and kept, the newest word on each standing.

Pure functions, plus the one small job that keeps the details.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal

from ...engine.contracts import EventSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from .common import _events_of
from .statements import CAME_IN

DETAIL_EVENT = "donors.letter_detail"  # the letters' details, as said; newest wins per field
ACKNOWLEDGE_FROM = 25000  # cents: a single gift this size or more is acknowledged in writing
EIN = re.compile(r"^\d{2}-?\d{7}$")
_NAMED = re.compile(r"^\s*gifts?\s+from\s+(.+?)\s*$", re.I)
_LINE = "______________________________"


# ---- who gave ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gift:
    day: str  # YYYY-MM-DD
    cents: int
    donor: str
    fund_grant: bool


def donor_of(row: dict) -> str:
    """Who gave the money a bank row carries: as said, or the name a kind
    for one donor carries ("Gifts from <name>"); "" when no one is named."""
    said = str(row.get("gift_from") or "").strip()
    if said:
        return said
    named = _NAMED.match(str(row.get("category") or ""))
    return named[1] if named else ""


def gifts_of(rows: Iterable[dict]) -> list[Gift]:
    """Every gift among month_report bank rows, oldest first: the ministry's
    income with its donor named."""
    out = [
        Gift(
            str(r["date"]),
            int(Decimal(str(r["dollars"])) * 100),
            donor_of(r),
            bool(r.get("fund_grant")),
        )
        for r in rows
        if r.get("role") == CAME_IN and r.get("dollars") and donor_of(r)
    ]
    return sorted(out, key=lambda g: (g.day, g.donor.casefold()))


@dataclass
class Donor:
    name: str
    gifts: list[Gift] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(g.cents for g in self.gifts)

    @property
    def acknowledged(self) -> list[Gift]:
        """The gifts that each need their own written acknowledgment."""
        return [g for g in self.gifts if g.cents >= ACKNOWLEDGE_FROM]


def by_donor(gifts: Iterable[Gift]) -> tuple[list[Donor], list[Gift]]:
    """The people who gave, by name (the first spelling said stands, any
    case), and the grants from donor-advised funds apart from them."""
    people: dict[str, Donor] = {}
    funds = []
    for g in gifts:
        if g.fund_grant:
            funds.append(g)
            continue
        people.setdefault(g.donor.casefold(), Donor(g.donor)).gifts.append(g)
    return sorted(people.values(), key=lambda d: d.name.casefold()), funds


# ---- what a letter needs that the books don't hold -----------------------------------


@dataclass(frozen=True)
class Letterhead:
    ministry: str
    ein: str = ""
    address: str = ""
    signer: str = ""
    title: str = ""
    thanks: str = ""  # a sentence on what gifts did this year, in the ministry's words

    def missing(self) -> list[str]:
        """What a letter still needs, in the words Tim asks for it."""
        out = []
        if not self.ein:
            out.append("the ministry's EIN")
        if not self.address:
            out.append("the ministry's mailing address")
        if not (self.signer and self.title):
            out.append("who signs the letters, and their title")
        return out


_HEAD = tuple(f.name for f in fields(Letterhead) if f.name != "ministry")


def letterhead(said: Iterable[dict], ministry: str) -> Letterhead:
    """The letters' details from every word said, the newest on each standing."""
    kept: dict[str, str] = {}
    for p in said:
        if p.get("donor"):
            continue
        kept |= {k: str(p[k]) for k in _HEAD if p.get(k)}
    return Letterhead(ministry, **kept)


def address_of(said: Iterable[dict], donor: str) -> str:
    """A donor's mailing address, the newest said, any case of their name."""
    found = ""
    for p in said:
        if str(p.get("donor") or "").casefold() == donor.casefold() and p.get("address"):
            found = str(p["address"])
    return found


# ---- the letters --------------------------------------------------------------------


def long_day(day: str) -> str:
    """A day as a letter says it: "January 6, 2026"."""
    d = date.fromisoformat(day)
    return f"{d:%B} {d.day}, {d.year}"


def usd(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def _lines(address: str) -> list[str]:
    """An address as it is printed: the lines said, or the street then the rest."""
    if "\n" in address:
        return [p.strip() for p in address.splitlines() if p.strip()]
    street, _comma, rest = address.partition(",")
    return [p.strip() for p in (street, rest) if p.strip()]


def _top(head: Letterhead, today: str, to: str, address: str) -> list[tuple]:
    out: list[tuple] = [("title", head.ministry)]
    out.append(("small", "  |  ".join(p for p in (head.address, f"EIN {head.ein}") if p)))
    out += [("gap",), ("text", long_day(today)), ("gap",), ("text", to)]
    out += [("text", line) for line in _lines(address)]
    return [*out, ("gap",)]


def _paragraphs(*texts: str) -> list[tuple]:
    """Paragraphs with a little space after each."""
    out: list[tuple] = []
    for t in texts:
        out += [("text", t), ("gap",)]
    return out


def _ends(name: str) -> str:
    """A name that ends a sentence: "Example Inc." keeps its one period."""
    return name if name.endswith(".") else f"{name}."


def _close(head: Letterhead) -> list[tuple]:
    return [
        ("text", "With gratitude,"),
        ("gap",),
        ("text", _LINE),
        ("text", head.signer),
        ("text", f"{head.title}, {head.ministry}"),
    ]


def _greeting(name: str) -> str:
    """ "Dear Ruth," for a person; a family or a group keeps its whole name."""
    words = name.split()
    first = words[0] if len(words) == 2 and words[-1].casefold() != "family" else name
    return f"Dear {first},"


def _acknowledges(
    name: str, gifts: list[Gift], head: Letterhead, opening: str, today: str, address: str
) -> list[tuple]:
    one = len(gifts) == 1
    these = "this contribution" if one else "these contributions"
    out = _top(head, today, name, address)
    out += _paragraphs(
        _greeting(name),
        opening,
        f"This letter acknowledges the following cash contribution{'' if one else 's'}, "
        "received in the organization's bank account:",
    )
    rows = [(long_day(g.day), usd(g.cents), False) for g in gifts]
    if not one:
        rows.append(("Total", usd(sum(g.cents for g in gifts)), True))
    out += [("table", "", rows), ("gap",)]
    out += _paragraphs(
        f"No goods or services were provided in exchange for {these}.",
        f"{head.ministry} has exclusive legal control over the use of {these}.",
        f"{head.ministry} is a tax-exempt organization under section 501(c)(3) of the "
        f"Internal Revenue Code, EIN {head.ein}.",
        f"This letter covers only gifts made directly to {_ends(head.ministry)} Grants from a "
        "donor-advised fund are acknowledged by the fund's sponsor and are not included here.",
    )
    return out + _close(head)


def statement(donor: Donor, head: Letterhead, year: str, today: str, address: str) -> list[tuple]:
    """The donor's year-end giving statement: every gift that year."""
    opening = f"Thank you for your generous support of {head.ministry} in {year}."
    if head.thanks:
        opening += f" {head.thanks}"
    return _acknowledges(donor.name, donor.gifts, head, opening, today, address)


def acknowledgment(gift: Gift, head: Letterhead, today: str, address: str) -> list[tuple]:
    """The written acknowledgment of one gift of $250 or more."""
    opening = f"Thank you for your generous gift to {_ends(head.ministry)}"
    return _acknowledges(gift.donor, [gift], head, opening, today, address)


def thank_you(gift: Gift, head: Letterhead, today: str, address: str) -> list[tuple]:
    """A thank-you for a grant from a donor-advised fund: no tax language,
    since the fund's sponsor gave the donor their receipt."""
    out = _top(head, today, gift.donor, address)
    out += _paragraphs(
        f"Dear friends at {gift.donor},",
        f"Thank you for the grant of {usd(gift.cents)} received on {long_day(gift.day)}. "
        f"We are grateful to you and to the donor who recommended it to {_ends(head.ministry)}",
        *([head.thanks] if head.thanks else []),
    )
    return out + _close(head)


# ---- the job that keeps the details ---------------------------------------------------

_SAID = (*_HEAD, "donor")


def detail_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "donordetail")
    for name in (*_SAID, "by"):
        key.param(name)
    key.add("before", len(_events_of(ctx, DETAIL_EVENT)))
    return key.digest()


def detail_run(ctx: JobContext) -> JobOutput:
    """Keep what the letters need, as said (the door checked an EIN's shape)."""
    payload = {k: str(ctx.params[k]).strip() for k in _SAID if ctx.params.get(k)}
    if not payload:
        return JobOutput(status="ok", summary="donor letter details: nothing said")
    payload["by"] = str(ctx.params.get("by") or "")
    n = len(_events_of(ctx, DETAIL_EVENT))
    return JobOutput(
        status="ok",
        summary="donor letter details: kept",
        events=[EventSpec(key=f"donor-detail:{n}", event_type=DETAIL_EVENT, payload=payload)],
    )
