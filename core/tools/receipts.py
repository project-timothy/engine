"""Receipts in from the person's own phone (Tim's door; the owner's design of
2026-10-12, for the alpha missionary).

"Hey Tim, here's a $43 receipt": the person's AI calls ``add_receipt`` with
their own words in ``said``, and code reads the amount and its currency from
those words (``claim_in``); the AI never types an amount (invariant 2). Tim
answers with a one-use link, good for fifteen minutes (``ReceiptDesk``), to
a page on the box's own door that opens the phone's camera. The photo lands
in the person's own folder of the expense drop tree, under the ministry's
one fund (``FUND``) so no "which project?" card stops it, with the claim in
its name, where intake already reads a ``$43.00`` tag as a claim to check;
then intake runs at once, so "it's in with this month's receipts" is true.

Adding a receipt commits no money (the month's report is the yes), so the
link takes no Face ID. A currency other than dollars is kept as said
("4,300.00 rupees") and never written as a dollar tag; its dollar value is
set with the monthly report. Standard library only (invariant 9).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agents.expenses import statements
from ..agents.expenses.categories import UNCATEGORIZED, canonical, from_words
from ..authority import Request, Role, evaluate
from ..engine import private_file
from ..engine.clock import local_today
from ..engine.config import load_tenant, tenant_dir
from ..engine.keyfile import add_key, drop_keys, person_for_key
from .viewer import unit_of

FUND = "General"
"""The folder a door receipt files under: a ministry's one fund, until the
nonprofit fund design gives a missionary more than one."""

TTL_SECONDS = 15 * 60
PILE_TTL_SECONDS = 24 * 60 * 60  # a catch-up link: a day, for as many sends as it takes
READ_AT_ONCE = 5  # receipts handed to the person's AI in one answer
MAX_SHOWN = 3_500_000  # bytes; under the 5 MB an image may be once encoded
_SHOWN = {".jpg": "image/jpeg", ".png": "image/png"}
MAX_PHOTO = 10 * 1024 * 1024
MAX_PICK = 64  # bytes: her tap on a reading's question ("pick=right")
PAGE_PATH = "/receipt"

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
_DOLLAR_WORDS = frozenset({"dollar", "dollars", "usd", "buck", "bucks"})
_CODES = frozenset(
    "USD EUR GBP CAD AUD NZD CHF JPY CNY KRW INR NPR PKR BDT LKR MNT KHR THB VND IDR MYR PHP "
    "MZN KES UGX TZS RWF ETB ZAR ZMW MWK NGN GHS XOF XAF MXN GTQ HNL BRL PEN".split()
)
_LOCAL_WORDS = frozenset(
    "rupee rupees meticais metical meticals shilling shillings peso pesos naira cedi cedis "
    "kwacha birr franc francs rand baht riel tugrik sol soles quetzal quetzales lempira "
    "lempiras taka".split()
)
# A currency word the person used, settled by where they are (alpha step 3,
# 2026-10-13): "rupees" from Kathmandu is Nepal's rupee. A word with one
# meaning maps straight to its code; a word several countries share maps by
# the person's own time zone; anywhere else it stays unsettled, and the
# review card says so rather than guess.
_WORD_CODES: dict[str, str | dict[str, str]] = {
    "rupee": {
        "Asia/Kathmandu": "NPR",
        "Asia/Katmandu": "NPR",
        "Asia/Kolkata": "INR",
        "Asia/Calcutta": "INR",
        "Asia/Karachi": "PKR",
        "Asia/Colombo": "LKR",
    },
    "metical": "MZN",
    "shilling": {
        "Africa/Nairobi": "KES",
        "Africa/Kampala": "UGX",
        "Africa/Dar_es_Salaam": "TZS",
    },
    "peso": {"America/Mexico_City": "MXN", "Asia/Manila": "PHP"},
    "naira": "NGN",
    "cedi": "GHS",
    "kwacha": {"Africa/Lusaka": "ZMW", "Africa/Blantyre": "MWK"},
    "birr": "ETB",
    "rand": "ZAR",
    "baht": "THB",
    "riel": "KHR",
    "tugrik": "MNT",
    "sol": "PEN",
    "quetzal": "GTQ",
    "lempira": "HNL",
    "taka": "BDT",
}
_WORD_STEMS = {
    "rupees": "rupee",
    "meticais": "metical",
    "meticals": "metical",
    "shillings": "shilling",
    "pesos": "peso",
    "cedis": "cedi",
    "soles": "sol",
    "quetzales": "quetzal",
    "lempiras": "lempira",
}


def settled(claim: Claim, zone: str) -> Claim:
    """The claim with its currency code filled in from the word the person
    used and where they are, when that settles it; otherwise unchanged."""
    if claim.currency:
        return claim
    word = claim.unit.casefold()
    codes = _WORD_CODES.get(_WORD_STEMS.get(word, word))
    code = codes if isinstance(codes, str) else (codes or {}).get(zone, "")
    return Claim(claim.amount, code, claim.unit) if code else claim


def local_currency(zone: str) -> str:
    """The money of the place on the person's clock, when the table above
    knows it (Asia/Kathmandu: NPR); "" otherwise."""
    for codes in _WORD_CODES.values():
        if isinstance(codes, dict) and zone in codes:
            return codes[zone]
    return ""


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATES = re.compile(
    rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.IGNORECASE,
)


_MONTH_DAY = re.compile(
    rf"\b({_MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b"
    rf"|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTHS})|\b\d{{4}}-(\d{{2}}-\d{{2}})\b",
    re.IGNORECASE,
)
_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def days_named(said: str) -> set[str]:
    """Each day their words name, as MM-DD ("march 7", "7th of March",
    "2026-03-07" all give "03-07"); a word like "yesterday" names none."""
    out = set()
    for m in _MONTH_DAY.finditer(said):
        if m[5]:
            out.add(m[5])
        else:
            month = _MONTH_NAMES.index((m[1] or m[4])[:3].casefold()) + 1
            out.add(f"{month:02d}-{int(m[2] or m[3]):02d}")
    return out


def slip_amount(said: str) -> Decimal | None:
    """The one number in an ATM slip's words once its dates are set aside
    ("Feb 14 gave 26,500"); None when none, or more than one, is left. Only
    where Tim asked how much an ATM gave: anywhere else a bare number is
    never an amount."""
    numbers = re.findall(rf"(?<![\d,.])(?:{_NUMBER})(?![\d,.])", _DATES.sub(" ", said))
    return Decimal(numbers[0].replace(",", "")) if len(numbers) == 1 else None


# "4,300 Nepali rupees": the country names the currency, wherever they are.
_QUALIFIED = {
    ("nepali", "rupee"): "NPR",
    ("nepalese", "rupee"): "NPR",
    ("indian", "rupee"): "INR",
    ("pakistani", "rupee"): "PKR",
    ("sri lankan", "rupee"): "LKR",
    ("kenyan", "shilling"): "KES",
    ("ugandan", "shilling"): "UGX",
    ("tanzanian", "shilling"): "TZS",
    ("mexican", "peso"): "MXN",
    ("philippine", "peso"): "PHP",
    ("filipino", "peso"): "PHP",
    ("zambian", "kwacha"): "ZMW",
    ("malawian", "kwacha"): "MWK",
}


def currency_name(code: str) -> str:
    """'NPR' -> 'Nepali rupees', from the names people use above; '' when
    this table doesn't know the code."""
    for (country, unit), known in _QUALIFIED.items():
        if known == code:
            return f"{country.title()} {unit if unit.endswith('a') else unit + 's'}"
    return ""


_QUALIFIED_RE = re.compile(
    rf"({_NUMBER})\s?(nepali|nepalese|indian|pakistani|sri lankan|kenyan|ugandan|tanzanian|"
    r"mexican|philippine|filipino|zambian|malawian)\s+([a-z]+)\b",
    re.IGNORECASE,
)

_BEFORE = re.compile(rf"([$€£₹])\s?({_NUMBER})|\b([A-Z]{{3}})\s?({_NUMBER})")
_AFTER = re.compile(rf"({_NUMBER})\s?([A-Za-z]+)\b")


_NO_DONOR = {
    "status": "asks",
    "say": "Only money in on the ministry's own account has a giver: a donor who gave to the "
    "ministry, or someone who gave it to them as a gift. A line they spent never does.",
}


def _whose_done(
    line: dict, pays: str | None, theirs_in: bool, own: bool, giver: str = ""
) -> dict | None:
    """What Tim says once a line is said to pay someone back, to be their own
    money in (c31), or to be their own spending; None for any other line."""
    if pays:
        dollars = Decimal(-int(line["cents"])) / 100
        return {
            "status": "set",
            "category": "",
            "say": f"Done: the {_day_words(str(line['day']))} line paid {pays} back "
            f"${dollars:,.2f}. What the ministry owes them comes down by it; it never counts "
            "as spending twice.",
        }
    if theirs_in and giver:
        dollars = Decimal(int(line["cents"])) / 100
        return {
            "status": "set",
            "category": "",
            "say": f"Done: the {_day_words(str(line['day']))} deposit of ${dollars:,.2f} was a "
            f"gift from {giver} for them: in their fund, never the ministry's income, and kept "
            "off their taxes. Nobody else's deposits count as from them.",
        }
    if theirs_in:
        dollars = Decimal(int(line["cents"])) / 100
        return {
            "status": "set",
            "category": "",
            "say": f"Done: the {_day_words(str(line['day']))} deposit of ${dollars:,.2f} was "
            "their own money, so it's in their fund and never the ministry's income. Their "
            "pay and what they spend on themselves come out of it.",
        }
    if own:
        dollars = Decimal(-int(line["cents"])) / 100
        return {
            "status": "set",
            "category": "",
            "say": f"Done: the {_day_words(str(line['day']))} line was their own, so "
            f"${dollars:,.2f} is owed to the ministry. It never counts as the ministry's "
            "spending.",
        }
    return None


def _whose_refused(line: dict, own_fits: bool, own: bool, from_pay: bool) -> dict | None:
    """Why a line can't be what whose says, or None when it can: their own
    only on the ministry's account (owed to it, or their money in), and paid
    from their pay only on their own account's spending (c33)."""
    on_ministry = line.get("account") == statements.MINISTRY
    if from_pay and (on_ministry or statements.Line.of(line).kind != statements.OUT):
        return {
            "status": "asks",
            "say": "Only a ministry cost on their own account, picked from their own "
            "statement, can be paid from their pay. On the ministry's account, ask whether "
            "it was the ministry's or their own.",
        }
    if own and (not on_ministry or not own_fits):
        return {
            "status": "asks",
            "say": "Only a line on the ministry's own account can be their own: spending "
            "owed back to it, or money in that was theirs. On their own statement, a line "
            "they don't pick never counts.",
        }
    return None


def _pay_done(line: dict, name: str, pay: Any) -> dict:
    """The words for a line kept as someone's pay (c34), with the month's pay
    the board set when the bank shows something else."""
    cents = abs(int(line["cents"]))
    month = date.fromisoformat(str(line["day"])).strftime("%B")
    said = (
        f"Done: the {_day_words(str(line['day']))} line was {name}'s pay for {month}, "
        f"${cents / 100:,.2f}."
    )
    if cents != pay.cents:
        said += (
            f" The board set ${pay.cents / 100:,.2f} for {month}, and the books keep what "
            "the bank shows. Whoever pays the ministry's bills will want to know."
        )
    return {"status": "set", "say": said}


def _from_pay_done(line: dict, from_pay: bool, category: str) -> dict | None:
    """What Tim says once a line on their own account was paid from their
    own pay (c33); None for any other line."""
    if not from_pay:
        return None
    dollars = Decimal(-int(line["cents"])) / 100
    return {
        "status": "set",
        "category": category,
        "say": f"Done: the {_day_words(str(line['day']))} line of ${dollars:,.2f} was paid "
        "from their own pay, so the ministry owes them nothing for it. It comes off their "
        "1099 income on their tax packet.",
    }


def _gift_params(giver: str, grant: bool, fund: str = "") -> dict:
    """Who gave a deposit, and whose fund it was given toward, for the
    line_purpose job (c32, c34 PR 3)."""
    if not giver:
        return {}
    return {
        "gift_from": giver,
        **({"fund_grant": "yes"} if grant else {}),
        **({"for_fund": fund} if fund else {}),
    }


_KIN = re.compile(
    r"\b(mom|mother|dad|father|parents?|grand\w*|brother|sister|sons?|daughters?|aunt|uncle"
    r"|wife|husband|cousin|\w+-in-law)\b",
    re.IGNORECASE,
)


def _last_name(name: str) -> str:
    words = [w for w in re.findall(r"[a-z']+", name.casefold()) if w not in ("the", "family")]
    return words[-1] if words else ""


def _fund_wall(policy: Any, who: str, giver: str, not_family: bool) -> dict | None:
    """The family wall (c34 PR 3): IRS Publication 526 says a gift set aside
    for a specific person isn't a charitable gift, and its own example is a
    parent paying a missionary child's costs. Refused when the giver is the
    person or their family; asked once when the giver shares their last
    name; None when the gift may count toward their fund."""
    shown = policy.display(who)
    kin = policy.person_named(giver)
    if kin == who or kin in policy.family_of(who) or _KIN.search(giver):
        return {
            "status": "refused",
            "say": f"{giver} is {shown} or their family, so the gift can't count toward "
            f"{shown}'s fund: IRS Publication 526 says a gift set aside for a specific person "
            "isn't a charitable gift, and its own example is a parent paying a missionary "
            "child's costs. If it was for them, explain the line as whose own with gift_from "
            "naming the giver: a gift for them, kept off their taxes. If it was for the "
            "ministry's work, leave for_fund off.",
        }
    if not not_family and _last_name(giver) and _last_name(giver) == _last_name(shown):
        return {
            "status": "asks",
            "say": f"{giver} shares {shown}'s last name. Ask whether they're family: family "
            "can't give toward a missionary's fund (IRS Publication 526). If they aren't, pass "
            "not_family.",
        }
    return None


def _gift_done(
    day: str, cents: int, giver: str, grant: bool, category: str, fund: str = ""
) -> dict:
    """What Tim says once a deposit names who gave it (c32), and whose fund
    it was given toward (c34 PR 3)."""
    what = "grant" if grant else "gift"
    then = (
        ". A grant from a donor-advised fund gets a thank-you; the fund gave the donor their "
        "receipt."
        if grant
        else ". It goes on their giving statement for the year."
    )
    under = f", under {category}" if category else ""
    toward = f" toward {fund}'s fund" if fund else ""
    return {
        "status": "set",
        "category": category,
        "say": f"Done: the {day} {what} of ${Decimal(cents) / 100:,.2f} is from "
        f"{giver}{toward}{under}{then}",
    }


@dataclass(frozen=True)
class Claim:
    """How much the person said the receipt was, and in what: ``currency``
    an ISO code when the words make it plain, else ""; ``unit`` as said."""

    amount: Decimal
    currency: str
    unit: str

    def words(self) -> str:
        """The claim the way a person reads it back."""
        if self.currency == "USD":
            return f"${self.amount:,.2f}"
        if self.unit in _SYMBOLS:
            return f"{self.unit}{self.amount:,.2f}"
        if self.currency:
            return f"{self.currency} {self.amount:,.2f}"
        return f"{self.amount:,.2f} {self.unit}"

    def tag(self) -> str:
        """The claim in a file name: intake's ``$43.00`` amount tag for
        dollars, and never a dollar tag for anything else."""
        if self.currency == "USD":
            return f"${self.amount:.2f}"
        if self.currency:
            return f"{self.currency}{self.amount:.2f}"
        return f"{self.amount:.2f}-{unit_of(self.unit)}"


def _claim(number: str, currency: str, unit: str) -> Claim:
    return Claim(Decimal(number.replace(",", "")).quantize(Decimal("0.01")), currency, unit)


_AMOUNT_LIKE = re.compile(
    r"(?<![\w.,/:-])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2}|\d{2,})(?![\w/:-])"
)


def _bare_number(said: str) -> str:
    """The amount-like number in words that name no money ("12.49 total"),
    or "": a date, a time, or a receipt number isn't one. Only for asking
    which money; a bare number is never kept as an amount (claim_in)."""
    if claim_in(said) is not None:
        return ""
    found = [m.group(1) for m in _AMOUNT_LIKE.finditer(said)]
    return found[0] if len(found) == 1 else ""


def claim_in(said: str) -> Claim | None:
    """The first amount in the person's words that says what it is in: a
    symbol ($43), a currency code (NPR 4300, 1,250 MZN), or a word (43
    dollars, 4,300 rupees). A bare number is never an amount: "receipt 43"
    or "Oct 3" could be anything, so Tim asks."""
    found: list[tuple[int, Claim]] = []
    for m in _QUALIFIED_RE.finditer(said):
        word = m.group(3).casefold()
        code = _QUALIFIED.get((m.group(2).casefold(), _WORD_STEMS.get(word, word)))
        if code:
            found.append((m.start(), _claim(m.group(1), code, f"{m.group(2)} {m.group(3)}")))
    for m in _BEFORE.finditer(said):
        if m.group(1):
            found.append((m.start(), _claim(m.group(2), _SYMBOLS[m.group(1)], m.group(1))))
        elif m.group(3) in _CODES:
            found.append((m.start(), _claim(m.group(4), m.group(3), m.group(3))))
    for m in _AFTER.finditer(said):
        word = m.group(2)
        if word.casefold() in _DOLLAR_WORDS:
            found.append((m.start(), _claim(m.group(1), "USD", word)))
        elif word in _CODES:
            found.append((m.start(), _claim(m.group(1), word, word)))
        elif word.casefold() in _LOCAL_WORDS:
            found.append((m.start(), _claim(m.group(1), "", word)))
    return min(found, key=lambda f: f[0])[1] if found else None


_BARE = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\w,])")


def _amount_said(said: str, currency: str) -> Decimal | None:
    """The amount a person takes out of a line, in the line's money: one
    that says its money (and it is the line's), else the one bare number in
    their words ("1,300 of that was mine": of that line). None when it says
    another money, or no number, or more than one."""
    claim = claim_in(said)
    if claim is not None:
        return claim.amount if claim.currency in ("", currency) else None
    found = _BARE.findall(said)
    if len(found) != 1:
        return None
    whole, cents = found[0]
    return Decimal(whole.replace(",", "") + (f".{cents}" if cents else ""))


def _money_words(currency: str, amount: Decimal) -> str:
    return (
        f"${amount:,.2f}" if currency == "USD" else statements.cash_words(f"{currency}{amount:.2f}")
    )


@dataclass(frozen=True)
class Held:
    """A statement or a school list sent through a catch-up link, parsed and
    held in memory until the person says whose account it is (t-statements).
    Nothing of it is written anywhere: a line reaches the books only when
    it's the ministry's, or the person picks it; a restart forgets it."""

    id: str
    person: str
    name: str
    kind: str  # STATEMENT, SCHOOL_LIST, or EXPORT
    rows: tuple
    by: str
    until: float
    service: str = ""  # an EXPORT's service, "" until the person names it
    left_out: tuple[str, ...] = ()  # an EXPORT's rows Tim didn't keep, by type
    file_name: str = ""  # the name her phone gave the file: read for whose it is, never said
    flagged: tuple[int, ...] | None = None  # rows Tim showed her as flagged; None: never shown


STATEMENT, SCHOOL_LIST = "bank statement", "school list"
EXPORT = "payment service export"
"""A payment service's export (c32, processors.py): PayPal, a giving app."""


@dataclass(frozen=True)
class Ticket:
    person: str  # their id in authority.toml
    name: str  # their name in the expense drop tree
    claim: Claim | None  # None: no amount in their words; read it or ask after
    said: str = ""  # their own words, for the receipt's category
    pile: bool = False  # a catch-up link: many receipts, for a day (t-catch-up)
    by: str = ""  # who asked for it: the person, or a reviewer for them


@dataclass(frozen=True)
class Asked:
    """A question the receipt page put to the person about one receipt
    (card R1): each answer she may tap (``choices``, a pick to the amount
    tag her tap makes her word, and the words it records as hers; an empty
    tag sets nothing), the category the photo read, and whose it is."""

    person: str
    sha: str
    choices: dict[str, tuple[str, str]]
    category: str
    until: float
    no_day: bool = False
    """The receipt showed no day it was paid: her answer is asked the day."""


def _key(token: str) -> str:
    """A link as the desk keeps it: its hash, never the link itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ticket_to(t: Ticket) -> dict:
    claim = None if t.claim is None else [str(t.claim.amount), t.claim.currency, t.claim.unit]
    return {
        "person": t.person,
        "name": t.name,
        "claim": claim,
        "said": t.said,
        "pile": t.pile,
        "by": t.by,
    }


def _ticket_from(d: dict) -> Ticket:
    c = d["claim"]
    claim = None if c is None else Claim(Decimal(c[0]), c[1], c[2])
    return Ticket(d["person"], d["name"], claim, d["said"], pile=d["pile"], by=d["by"])


def _asked_to(a: Asked) -> dict:
    return {
        "person": a.person,
        "sha": a.sha,
        "choices": {k: list(v) for k, v in a.choices.items()},
        "category": a.category,
        "until": a.until,
        "no_day": a.no_day,
    }


def _asked_from(d: dict) -> Asked:
    choices = {k: (v[0], v[1]) for k, v in d["choices"].items()}
    return Asked(
        d["person"], d["sha"], choices, d["category"], float(d["until"]), no_day=d["no_day"]
    )


class ReceiptDesk:
    """The box's receipt links: a token, good once, for fifteen minutes; or
    a catch-up token, good for a day and for every send in it.

    With ``state`` (a file beside the books, core.engine.private_file), the
    links, the spent ones, and the receipt page's questions are kept there
    too, so a room that naps and wakes (shot 4, card H3) still honors the
    link in her hand; each by its hash, so the file holds no usable link.
    Without it, memory only, and a restart means "ask Tim again". A held
    statement or a proposed one stays in memory either way: nothing of a
    bank file is written before she says whose it is."""

    def __init__(
        self,
        *,
        ttl: float = TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        state: Path | None = None,
    ):
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._state = Path(state) if state is not None else None
        self._open: dict[str, tuple[Ticket, float]] = {}  # each by _key(token)
        self._used: dict[str, float] = {}  # _key(token) -> when it was spent
        self._held: dict[str, Held] = {}
        self._asked: dict[str, Asked] = {}  # each by _key(token)
        self._proposed: dict[str, tuple[Ticket, tuple, float, str]] = {}
        if self._state is not None and self._state.is_file():
            self._load()

    # ---- kept across a nap (card H3)

    def _load(self) -> None:
        kept = json.loads(self._state.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        self._open = {k: (_ticket_from(t), float(until)) for k, (t, until) in kept["open"].items()}
        self._used = {k: float(at) for k, at in kept["used"].items()}
        self._asked = {k: _asked_from(a) for k, a in kept["asked"].items()}

    def _save(self) -> None:
        """Under the lock: what's live, and what spent or ran out within a
        day (so "used" and "expired" still say so), to the state file."""
        if self._state is None:
            return
        gone = self.clock() - PILE_TTL_SECONDS
        self._open = {k: v for k, v in self._open.items() if v[1] >= gone}
        self._used = {k: at for k, at in self._used.items() if at >= gone}
        self._asked = {k: a for k, a in self._asked.items() if a.until >= gone}
        kept = {
            "open": {k: [_ticket_to(t), until] for k, (t, until) in self._open.items()},
            "used": self._used,
            "asked": {k: _asked_to(a) for k, a in self._asked.items()},
        }
        private_file.keep(self._state, json.dumps(kept, sort_keys=True))

    def propose(self, ticket: Ticket, rows: tuple, name: str = "") -> str:
        """A one-use token for her tap on a statement Tim read and code
        proved adds up (card R2), good for as long as a receipt link is.
        The lines wait here, in memory, until she keeps them, with the
        file's name for whose account it says it is."""
        token = secrets.token_urlsafe(18)
        with self._lock:
            now = self.clock()
            for gone in [k for k, p in self._proposed.items() if p[2] < now]:
                self._proposed.pop(gone)
            self._proposed[token] = (ticket, rows, now + self.ttl, name)
        return token

    def proposed(self, token: str) -> bool:
        """Whether the token is a statement waiting on her tap, without
        spending it."""
        with self._lock:
            return token in self._proposed

    def accept(self, token: str) -> tuple[Ticket, tuple, str] | None:
        """The proposed statement and its file's name, once, while it's
        good; None after."""
        with self._lock:
            held = self._proposed.pop(token, None)
        if held is None or self.clock() > held[2]:
            return None
        return held[0], held[1], held[3]

    def ask(
        self,
        person: str,
        sha: str,
        choices: dict[str, tuple[str, str]],
        category: str = "",
        *,
        no_day: bool = False,
    ) -> str:
        """A one-use token for the page's question about a receipt it just
        read (card R1), good for as long as a receipt link is."""
        token = secrets.token_urlsafe(18)
        with self._lock:
            now = self.clock()
            for gone in [k for k, a in self._asked.items() if a.until < now]:
                self._asked.pop(gone)
            self._asked[_key(token)] = Asked(
                person, sha, dict(choices), category, now + self.ttl, no_day=no_day
            )
            self._save()
        return token

    def answer(self, token: str) -> tuple[Asked | None, str]:
        """The question, once: (asked, "") or (None, "expired" | "unknown")."""
        with self._lock:
            asked = self._asked.pop(_key(token), None)
            self._save()
            if asked is None:
                return None, "unknown"
            return (asked, "") if self.clock() <= asked.until else (None, "expired")

    def issue(self, person: str, name: str, claim: Claim | None, said: str = "") -> str:
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._open[_key(token)] = (Ticket(person, name, claim, said), self.clock() + self.ttl)
            self._save()
        return token

    def issue_pile(self, person: str, name: str, *, by: str) -> str:
        token = secrets.token_urlsafe(18)
        ticket = Ticket(person, name, None, pile=True, by=by)
        with self._lock:
            self._open[_key(token)] = (ticket, self.clock() + PILE_TTL_SECONDS)
            self._save()
        return token

    def is_pile(self, token: str) -> bool:
        """Whether the token is a live catch-up link, without spending it."""
        with self._lock:
            held = self._open.get(_key(token))
            return bool(held and held[0].pile and self.clock() <= held[1])

    def hold(self, ticket: Ticket, kind: str, rows: tuple, **more: Any) -> Held:
        """A parsed file, held for a day for the person's word."""
        held = Held(
            "st-" + secrets.token_urlsafe(6),
            ticket.person,
            ticket.name,
            kind,
            rows,
            ticket.by,
            self.clock() + PILE_TTL_SECONDS,
            **more,
        )
        with self._lock:
            self._held[held.id] = held
        return held

    def held(self, person: str) -> list[Held]:
        """What waits for the person's word, oldest first."""
        with self._lock:
            now = self.clock()
            for gone in [k for k, h in self._held.items() if h.until < now]:
                self._held.pop(gone)
            return [h for h in self._held.values() if h.person == person]

    def drop(self, held_id: str) -> None:
        with self._lock:
            self._held.pop(held_id, None)

    def shown(self, held_id: str, flagged: tuple[int, ...]) -> None:
        """The rows of her own statement Tim showed her as flagged
        (t-keep-flagged): the only ones "keep what you flagged" can keep."""
        with self._lock:
            held = self._held.get(held_id)
            if held is not None:
                self._held[held_id] = replace(held, flagged=tuple(flagged))

    def gone(self, token: str) -> str:
        """Why the link no longer works ("used", "expired", "unknown"), or ""
        while it does, without spending it."""
        with self._lock:
            if _key(token) in self._used:
                return "used"
            held = self._open.get(_key(token))
            if held is None:
                return "unknown"
            return "" if self.clock() <= held[1] else "expired"

    def take(self, token: str) -> tuple[Ticket | None, str]:
        """The ticket, once (a catch-up ticket, until it expires):
        (ticket, "") or (None, "used" | "expired" | "unknown")."""
        with self._lock:
            if _key(token) in self._used:
                return None, "used"
            held = self._open.get(_key(token))
            if held is None:
                return None, "unknown"
            ticket, expires = held
            if ticket.pile and self.clock() <= expires:
                return ticket, ""
            self._open.pop(_key(token))
            self._used[_key(token)] = self.clock()
            self._save()
            return (ticket, "") if self.clock() <= expires else (None, "expired")


# ---- the receipt page on the home screen (chunk 10; the owner's yes of 2026-10-22)

HOME_PATH = "/receipt/k/"
LINKS_FILE = "receipt_links.toml"
HOME_PER_HOUR = 60
"""Photo tickets one standing link may take in an hour: far past a day of
receipts, short of anyone emptying a folder's worth of junk into intake."""

_LINKS_HEADER = "Home-screen receipt links' SHA-256 -> person (core/tools/receipts.py)."
_KEY = re.compile(r"^[A-Za-z0-9_-]{32}$")
_ICON = re.compile(r"^/receipt/icon-(180|192|512)\.png$")
_ASSETS = Path(__file__).resolve().parent / "assets"


def add_home_link(directory: Path, person: str) -> str:
    """A standing link for ``person``'s home screen: shown once, kept only as
    its hash; their older one stops working."""
    return add_key(Path(directory) / LINKS_FILE, person, header=_LINKS_HEADER, only_one=True)


def drop_home_link(directory: Path, person: str) -> int:
    """Retire ``person``'s home-screen link (the ministry's one line)."""
    return drop_keys(Path(directory) / LINKS_FILE, person, header=_LINKS_HEADER)


def home_person(directory: Path, key: str) -> str | None:
    """Whose standing link ``key`` is, or None."""
    return person_for_key(Path(directory) / LINKS_FILE, key) if _KEY.match(key) else None


def home_route(path: str) -> tuple[str, str] | None:
    """("page" | "manifest", key) or ("icon", size) for a home-screen path."""
    icon = _ICON.match(path)
    if icon:
        return "icon", icon.group(1)
    if not path.startswith(HOME_PATH):
        return None
    key, _, rest = path[len(HOME_PATH) :].partition("/")
    if not _KEY.match(key) or rest not in ("", "app.webmanifest"):
        return None
    return ("manifest" if rest else "page"), key


def icon(size: str) -> bytes:
    """Tim's mark at ``size`` pixels square, for the home screen."""
    return (_ASSETS / f"icon-{size}.png").read_bytes()


def manifest(key: str, ministry: str = "") -> dict:
    """The home screen's own entry: it opens straight to this person's page."""
    name = f"Tim receipts{f' for {ministry}' if ministry else ''}"
    return app_manifest(HOME_PATH + key, name, "Receipts")


class HomeLinks:
    """A standing link's one power: each time its page opens, an ordinary
    one-use photo ticket for its person (``ReceiptDesk.issue``), at most
    ``per_hour`` an hour. The first open from the home screen tells setup
    the step works (``on_home``)."""

    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        desk: ReceiptDesk,
        per_hour: int = HOME_PER_HOUR,
        person_for: Callable[[str], str | None] | None = None,
        name_for: Callable[[str], str] | None = None,
        on_home: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        root = Path(tenants_root) if tenants_root else None
        self.desk = desk
        self.per_hour = per_hour
        self.person_for = person_for or (
            lambda key: home_person(tenant_dir(tenant, tenants_root=root), key)
        )
        self.name_for = name_for or (lambda person: filer_name(tenant, root, person))
        self.on_home = on_home
        self.clock = clock
        self._lock = threading.Lock()
        self._taken: dict[str, list[float]] = {}

    def _rationed(self, key: str) -> bool:
        tag = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            now = self.clock()
            recent = [t for t in self._taken.get(tag, []) if now - t < 3600]
            if len(recent) >= self.per_hour:
                self._taken[tag] = recent
                return True
            self._taken[tag] = [*recent, now]
            return False

    def ticket(self, key: str, *, standalone: bool) -> tuple[int, dict]:
        person = self.person_for(key)
        name = self.name_for(person) if person else ""
        if not person or not name:
            return 404, {"say": _HOME_GONE}
        if self._rationed(key):
            return 429, {"say": _HOME_BUSY}
        if standalone and self.on_home is not None:
            self.on_home(person)
        return 200, {"ticket": self.desk.issue(person, name, None)}


_HOME_BUSY = "That's a lot of receipts for one hour. Try again in a little while."
_HOME_GONE = (
    "This receipt page no longer works. Ask Tim for a new one, or ask your ministry to "
    "set it up again."
)


_PLACERS = ("approve", "review")
"""Who places someone else's receipt: whoever approves or reviews their month."""


class Receipts:
    """``add_receipt`` for one signed-in person: offered when the ministry
    keeps an expense drop tree and the person files expenses there."""

    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        ledger_root: Path,
        person: str,
        desk: ReceiptDesk,
        base_url: str,
    ):
        self.tenant = tenant
        self.tenants_root = Path(tenants_root) if tenants_root else None
        self.ledger_root = ledger_root
        self.person = person
        self.desk = desk
        self.base_url = base_url
        self.name = filer_name(tenant, self.tenants_root, person)

    def offered(self) -> bool:
        return bool(self.name)

    # -- set_category (the owner's decisions of 2026-10-13) ---------------------

    def sorts(self, viewer: Any) -> bool:
        """set_category is offered to someone who files expenses here, or who
        approves them."""
        if viewer is None:
            return False
        if self.offered():
            return True
        policy = viewer.policy
        held = policy.people[viewer.person].roles if viewer.person in policy.people else ()
        return any(
            g.matches(action, "expense.report")
            for action in _PLACERS
            for role in held
            for g in policy.roles.get(role, Role()).grants
        )

    def _may_place(self, viewer: Any, unit: str) -> bool:
        """Their own receipt, or one in a month they approve or review."""
        if unit == self.person and self.offered():
            return True
        return any(
            evaluate(
                viewer.policy, Request(viewer.person, action, "expense.report", scope=unit)
            ).allowed
            for action in _PLACERS
        )

    def set_category(self, t: Any, args: dict) -> dict:
        from ..agents.expenses.records import LANDED_EVENT
        from ..engine.runner import door_run as run

        said = str(args.get("said") or "")
        found = re.fullmatch(r"events#(\d+)", str(args.get("line") or "").strip())
        rows = (
            t._rows(
                "SELECT event_type, payload_json FROM events WHERE id = ? AND tenant = ?",
                (int(found[1]), t.tenant),
            )
            if found
            else []
        )
        if not rows or rows[0]["event_type"] != LANDED_EVENT:
            return {
                "status": "not_found",
                "say": "That isn't a receipt that can move. Find it in month_report and pass "
                "its source as line; a month already filed can't change.",
            }
        receipt = json.loads(rows[0]["payload_json"] or "{}")
        if not self._may_place(t.viewer, unit_of(str(receipt.get("person", "")))):
            return {
                "status": "refused",
                "say": "That receipt isn't theirs, and it isn't in a month they review.",
            }
        listed = list(
            load_tenant(self.tenant, tenants_root=self.tenants_root).expenses.category_accounts
        )
        whose = str(args.get("whose") or "").strip()
        if whose not in ("", statements.PAY, statements.MINISTRY):
            return {
                "status": "asks",
                "say": "Ask whether they paid it from their own pay (whose pay) or want the "
                "ministry to pay it back (whose ministry).",
            }
        asked = str(args.get("category") or "").strip()
        category = canonical(asked, listed) if asked or not whose else None
        if category is None and (asked or not whose):
            return {
                "status": "needs_category",
                "say": f"The ministry's categories are {', '.join(listed)}, or "
                f"{UNCATEGORIZED}. Ask which one they mean.",
            }
        done = run(
            self.tenant,
            "expenses",
            "categorize",
            params={
                "sha": str(receipt.get("sha256", "")),
                **({"category": category} if category is not None else {}),
                "by": self.person,
                "said": said,
                **({"whose": whose} if whose else {}),
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return {
                "status": "busy",
                "say": "The books are busy for a moment. Ask them to try again in a minute.",
            }
        day = str(receipt.get("file", ""))[:10]
        if whose == statements.PAY:
            return {
                "status": "placed",
                "say": f"Done: they paid the {day} receipt from their own pay, so nobody owes "
                "it back and it isn't the ministry's spending. It comes off their income on "
                "their tax packet as a cost. If the month is waiting for a yes, its card "
                "leaves it off. Tell them so.",
            }
        if whose == statements.MINISTRY:
            return {
                "status": "placed",
                "say": f"Done: the {day} receipt is the ministry's again, so it goes back on "
                "the month's card to be paid back. Tell them so.",
            }
        return {
            "status": "placed",
            "say": f"Done: the {day} receipt is under {category or UNCATEGORIZED}. If the "
            "month is waiting for a yes, its card shows the change. Tell them so.",
        }

    def add(self, said: str, t: Any = None, *, another: bool = False) -> dict:
        claim = claim_in(said)
        named = claim if claim is None or claim.currency else settled(claim, self._zone())
        if named is not None and not named.currency:
            return {
                "status": "needs_currency",
                "say": f"Ask them which currency {named.words()} was in (for example Nepali "
                "or Indian rupees), then call add_receipt again with their words.",
            }
        if named is not None and t is not None and not another:
            if kept := self._kept(t, named, days_named(said)):
                day, source = kept
                return {
                    "status": "already_kept",
                    "say": f"Tim already keeps their {named.words()} receipt from {day} "
                    f"(source {source}), so ask for no photo. If they're saying part of it "
                    "was theirs, call split_line with that source as line and their words. "
                    "If they say this is another receipt, call add_receipt again with "
                    "another set to true.",
                }
        link = f"{self.base_url}#{self.desk.issue(self.person, self.name, claim, said)}"
        bare = _bare_number(said)
        goes = (
            f"as {claim.words()}"
            if claim is not None
            else f"with no amount yet. They said {bare} with no money named: after the photo, "
            f"ask only whether {bare} was dollars or rupees (or the money where they are), "
            f"never how much again, and pass {bare} with their answer to set_amount. Never "
            "guess which money"
            if bare
            else "with no amount yet. After the photo, ask them how much it was (their page "
            "says what the photo reads, when Tim can read it) and pass their words to "
            "set_amount. Never guess an amount"
        )
        return {
            "status": "tap_to_add",
            "show": f"[Tap to add your receipt]({link})",
            "say": "Show the person the show line as it is: a Markdown link. Their tap "
            f"opens their camera, and the photo goes in with this month's receipts {goes}. "
            "The link works once, for 15 minutes.",
        }

    def _whose(self, t: Any, args: dict) -> tuple[str, str, dict | None]:
        """(person id, filer name, None), or a refusal: the signed-in
        person's own receipts, or, named, someone whose month they review."""
        asked = str(args.get("person") or "").strip()
        if not asked:
            if not self.offered():
                return (
                    "",
                    "",
                    {
                        "status": "not_found",
                        "say": "They don't file receipts here. Ask whose receipts these are.",
                    },
                )
            return self.person, self.name, None
        cfg = load_tenant(self.tenant, tenants_root=self.tenants_root)
        # Only the people they may act for: someone outside them reads exactly
        # like a name nobody has, never said back (the weekly attacker, 10/16).
        names = [p.name for p in cfg.expenses.persons if self._may_place(t.viewer, unit_of(p.name))]
        said = asked.casefold().split()
        exact = [n for n in names if asked.casefold() in (n.casefold(), unit_of(n))]
        # "Ruth" is Ruth Hollis: every word said is a word of one name.
        found = exact or [n for n in names if set(said) <= set(n.casefold().split())]
        if len(found) > 1:
            return (
                "",
                "",
                {
                    "status": "which",
                    "say": f"More than one person goes by {asked}: {', '.join(found)}. "
                    "Ask which one they meant.",
                },
            )
        name = found[0] if found else ""
        if not name:
            return (
                "",
                "",
                {
                    "status": "not_found",
                    "say": f"{asked} isn't someone whose receipts they send here. "
                    "Ask who they meant.",
                },
            )
        return unit_of(name), name, None

    def catch_up(self, t: Any, args: dict) -> dict:
        """One link for a whole pile of receipts (t-catch-up): the person's
        own, or, for a reviewer, the person whose month they look after."""
        person, name, refused = self._whose(t, args)
        if refused is not None:
            return refused
        token = self.desk.issue_pile(person, name, by=self.person)
        whose = "their" if person == self.person else f"{name}'s"
        return {
            "status": "tap_to_add",
            "show": f"[Tap to send your receipts]({self.base_url}?many#{token})",
            "say": "Show the show line as it is: a Markdown link. Their tap opens a page "
            f"where they choose {whose} receipts all at once, photos from the camera roll, "
            "PDFs, the statements a bank's website exports (CSV files), or a school list, "
            "and send them. The link works for a day, for as many sends as it takes. A "
            "statement waits for bank_statement: ask whose account it is. Each receipt "
            "waits for its own day and amount: walk them through them with month_report, "
            "and pass their words to set_receipt_day and set_amount. Never guess a day or "
            "an amount.",
        }

    def to_read(self, t: Any, args: dict) -> dict:
        """The next receipts waiting to be read, as images for the person's
        own AI to read (t-reader): sent without a day or an amount, not yet
        read. A file it can't show is named, for the person to say."""
        from ..agents.expenses import records
        from ..agents.expenses.records import AMOUNT_EVENT, LANDED_EVENT, READ_EVENT

        person, name, refused = self._whose(t, args)
        if refused is not None:
            return refused
        limit = max(1, min(int(args.get("limit") or 3), READ_AT_ONCE))
        read = {
            str(json.loads(r["payload_json"] or "{}").get("sha256"))
            for r in t._rows(
                "SELECT payload_json FROM events WHERE tenant = ? AND event_type IN (?, ?, ?)",
                (t.tenant, READ_EVENT, AMOUNT_EVENT, records.DATED_EVENT),
            )
        }
        filed = {
            str(r["receipt_sha256"])
            for r in t._rows(
                "SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,)
            )
        }
        waiting = []
        for r in t._rows(
            "SELECT id, payload_json FROM events WHERE tenant = ? AND event_type = ? ORDER BY id",
            (t.tenant, LANDED_EVENT),
        ):
            e = json.loads(r["payload_json"] or "{}")
            sha, file = str(e.get("sha256", "")), str(e.get("file", ""))
            unread = records.UNDATED in file or "_receipt_unread" in file
            if e.get("person") == name and unread and sha not in read | filed:
                waiting.append((r["id"], e))
        shown, content = [], []
        for eid, e in waiting[:limit]:
            path = Path(str(e.get("filed_to", "")))
            data = path.read_bytes() if path.is_file() else b""
            kind = kind_of(data)
            line = {"line": f"events#{eid}", "sent": str(e.get("file", ""))[:10]}
            if kind in _SHOWN and len(data) <= MAX_SHOWN:
                content.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(data).decode("ascii"),
                        "mimeType": _SHOWN[kind],
                    }
                )
                line["shown"] = f"image {len(content)}"
            elif kind == ".pdf" and len(data) <= MAX_SHOWN:
                content.append(
                    {
                        "type": "resource",
                        "resource": {
                            "uri": f"receipt:events/{eid}",
                            "mimeType": "application/pdf",
                            "blob": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
                line["shown"] = f"file {len(content)}, a PDF"
            else:
                line["shown"] = "can't be shown here: ask the person what it says"
            shown.append(line)
        return {
            "person": name,
            "receipts": shown,
            "more": max(0, len(waiting) - limit),
            "say": "Each receipt's image follows, in order. Read each one: its day, the "
            "amount and currency, who was paid, and what it was for; then pass that to "
            "record_reading with its line. A line is for your calls; never say it to the "
            "person: speak of a receipt by its day, amount, and who was paid. Read only "
            "what the receipt shows; leave out anything it doesn't. Then walk the person "
            "through what you read, a month at a time; the amount counts only on their yes "
            "(set_amount)."
            if shown
            else "Nothing is waiting to be read.",
            "_content": content,
        }

    def record_reading(self, t: Any, args: dict) -> dict:
        """What one receipt reads, from the person's own AI: a proposal (the
        photo's reading), never their word. The day files it in its month;
        the amount waits for their yes."""
        receipt, refused = self._landed(t, args)
        if refused is not None:
            return refused
        day = _day_of(args.get("day"), t._today())
        if day is None:
            return {
                "status": "not_a_day",
                "say": "Pass the day the receipt shows as day (YYYY-MM-DD). A day still to "
                "come can't be right; if it shows no day, ask the person.",
            }
        currency = str(args.get("currency") or "").strip().upper()
        try:
            amount = Decimal(str(args.get("amount"))).quantize(Decimal("0.01"))
        except ArithmeticError:
            amount = None
        claim = (
            Claim(amount, currency, "$" if currency == "USD" else currency)
            if amount is not None and amount > 0 and currency in _CODES
            else None
        )
        done = record_read(
            self.tenant,
            self.tenants_root,
            self.ledger_root,
            sha=str(receipt.get("sha256", "")),
            claim=claim,
            day=day,
            payee=str(args.get("payee") or ""),
            category=str(args.get("category") or ""),
            by=self.person,
        )
        if done == "error":
            return {
                "status": "busy",
                "say": "The books are busy for a moment. Try again in a minute.",
            }
        return {
            "status": "read",
            "say": f"Recorded what it reads, filed on {day:%b} {day.day}, {day.year}. Its "
            "amount counts once the person agrees to it.",
        }

    def _landed(self, t: Any, args: dict) -> tuple[dict, dict | None]:
        """The receipt a month_report line names, if the signed-in person may
        place it: (its landed payload, None), or a refusal."""
        from ..agents.expenses.records import LANDED_EVENT

        found = re.fullmatch(r"events#(\d+)", str(args.get("line") or "").strip())
        rows = (
            t._rows(
                "SELECT event_type, payload_json FROM events WHERE id = ? AND tenant = ?",
                (int(found[1]), t.tenant),
            )
            if found
            else []
        )
        if not rows or rows[0]["event_type"] != LANDED_EVENT:
            return {}, {
                "status": "not_found",
                "say": "That isn't a receipt waiting in a month. Find it in month_report and "
                "pass its source as line.",
            }
        receipt = json.loads(rows[0]["payload_json"] or "{}")
        if not self._may_place(t.viewer, unit_of(str(receipt.get("person", "")))):
            return {}, {
                "status": "refused",
                "say": "That receipt isn't theirs, and it isn't in a month they review.",
            }
        return receipt, None

    def agree(self, t: Any, args: dict) -> dict:
        """The person's one yes to every reading left in a month (the month
        walk): each amount as read becomes their word, and a category read
        places the receipt when nobody has placed it."""
        from ..agents.expenses import records
        from ..agents.expenses.records import AMOUNT_EVENT, CATEGORY_EVENT, LANDED_EVENT, READ_EVENT

        person, name, refused = self._whose(t, args)
        if refused is not None:
            return refused
        month = str(args.get("month") or "")
        said = str(args.get("said") or "")
        log = [
            {"event_type": r["event_type"], "payload": json.loads(r["payload_json"] or "{}")}
            for r in t._rows(
                "SELECT event_type, payload_json FROM events WHERE tenant = ? "
                "AND event_type IN (?, ?, ?, ?, ?) ORDER BY id",
                (
                    t.tenant,
                    LANDED_EVENT,
                    READ_EVENT,
                    AMOUNT_EVENT,
                    CATEGORY_EVENT,
                    records.DATED_EVENT,
                ),
            )
        ]
        dated = records.days(log)
        latest = {e["event_type"]: {} for e in log}
        for e in log:
            latest[e["event_type"]][str(e["payload"].get("sha256"))] = e["payload"]
        agreed = 0
        for sha, landed in latest.get(LANDED_EVENT, {}).items():
            reading = latest.get(READ_EVENT, {}).get(sha)
            if landed.get("person") != name or dated.get(sha, "")[:7] != month or not reading:
                continue
            agreed += agree_read(
                self.tenant,
                self.tenants_root,
                self.ledger_root,
                sha=sha,
                tag=str(reading["tag"]) if sha not in latest.get(AMOUNT_EVENT, {}) else "",
                category=""
                if sha in latest.get(CATEGORY_EVENT, {})
                else str(reading.get("category") or ""),
                said=said,
                by=self.person,
            )
        return {
            "status": "agreed",
            "receipts": agreed,
            "say": f"Done: {agreed} receipt{'s' if agreed != 1 else ''} counted as read. "
            "When the month looks right, send_month_for_review sends it for its yes.",
        }

    def send_month(self, t: Any, args: dict) -> dict:
        """A month to its reviewer as one card listing every line, when
        nothing in it still waits on a day, an amount, or a rate."""
        from ..engine.runner import door_run as run

        person, name, refused = self._whose(t, args)
        if refused is not None:
            return refused
        month = str(args.get("month") or "")
        if not re.fullmatch(r"\d{4}-\d{2}", month) or month > t._today().strftime("%Y-%m"):
            return {"status": "not_a_month", "say": "Pass a month that has come, as YYYY-MM."}
        mine = t.call("month_report", {"month": month, "person": name})["people"]
        if not mine or not (mine[0]["lines"] or mine[0]["drives"] or mine[0]["bank"]):
            return {"status": "empty", "say": f"Nothing of {name}'s is in that month."}
        if mine[0]["bank_waiting"]:
            n = mine[0]["bank_waiting"]
            return {
                "status": "waiting",
                "say": f"{n} bank line{'s' if n != 1 else ''} in that month still "
                f"wait{'s' if n == 1 else ''} on a receipt, or what it was for "
                "(month_report's bank lines say which; explain_bank_line takes their words). "
                "Nothing was sent.",
            }
        rate = "the day's exchange rate"  # the month's run asks for it
        waiting = sum(
            1 for ln in mine[0]["lines"] if ln["dollars"] is None and ln["waiting_on"] != rate
        )
        if waiting:
            return {
                "status": "waiting",
                "say": f"{waiting} receipt{'s' if waiting != 1 else ''} in that month still "
                "wait on a day, an amount, or a rate (month_report says which). Settle "
                "those first; nothing was sent.",
            }
        run(
            self.tenant,
            "expenses",
            "month",
            params={"month": month},
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        [now] = t.call("month_report", {"month": month, "person": name})["people"]
        if now["waiting_on_a_rate"]:
            return {
                "status": "waiting",
                "say": f"{now['waiting_on_a_rate']} receipt(s) in that month still wait on "
                "their day's exchange rate. Tim asks again each morning; nothing was sent.",
            }
        return {
            "status": "sent",
            "review": now["review"],
            "say": f"{name}'s month is with the reviewer for a yes: {now['receipts']} "
            f"receipt{'s' if now['receipts'] != 1 else ''} and {len(now['bank'])} bank "
            f"line{'s' if len(now['bank']) != 1 else ''}, ${now['total']} owed back. It's "
            f"{now['review'].lower()}.",
        }

    def set_day(self, t: Any, args: dict) -> dict:
        """A receipt's own day, in the person's words (t-catch-up): it files
        the receipt in that day's month, priced at that day's rate."""
        from ..engine.runner import door_run as run

        receipt, refused = self._landed(t, args)
        if refused is not None:
            return refused
        paid = _day_of(args.get("day"), t._today())
        if paid is None:
            return {
                "status": "not_a_day",
                "say": "Ask them which day the receipt is from, then pass it as day "
                "(YYYY-MM-DD) with their words in said. A day still to come can't be right.",
            }
        day = paid.isoformat()
        done = run(
            self.tenant,
            "expenses",
            "date",
            params={
                "sha": str(receipt.get("sha256", "")),
                "day": day,
                "said": str(args.get("said") or ""),
                "by": self.person,
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return {
                "status": "busy",
                "say": "The books are busy for a moment. Ask them to try again in a minute.",
            }
        return {
            "status": "set",
            "say": f"Done: that receipt is from {paid:%b} {paid.day}, {paid.year}, and it's "
            f"in {paid:%B}'s receipts now. Tell them so.",
        }

    def set_drive_purpose(self, t: Any, args: dict) -> dict:
        """What one drive was for, in the person's words (core/mileage)."""
        from ..agents.expenses.records import DRIVE_EVENT, drive_id
        from ..engine.runner import door_run as run

        said = str(args.get("said") or "").strip()
        found = re.fullmatch(r"events#(\d+)", str(args.get("line") or "").strip())
        rows = (
            t._rows(
                "SELECT event_type, payload_json FROM events WHERE id = ? AND tenant = ?",
                (int(found[1]), t.tenant),
            )
            if found
            else []
        )
        if not rows or rows[0]["event_type"] != DRIVE_EVENT or not said:
            return {
                "status": "not_found",
                "say": "That isn't a drive. Find it in month_report's drives and pass its source "
                "as line, with their words in said.",
            }
        drive = json.loads(rows[0]["payload_json"] or "{}")
        if not self._may_place(t.viewer, unit_of(str(drive.get("person", "")))):
            return {
                "status": "refused",
                "say": "That drive isn't theirs, and it isn't in a month they review.",
            }
        done = run(
            self.tenant,
            "expenses",
            "drive_purpose",
            params={"drive": drive_id(drive), "said": said, "by": self.person},
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return {
                "status": "busy",
                "say": "The books are busy for a moment. Ask them to try again in a minute.",
            }
        return {"status": "set", "say": "Done: that drive's purpose is in, as they said it."}

    def set_amount(self, t: Any, args: dict) -> dict:
        """How much a receipt added without an amount was, in the person's
        words: the amount they say, or their yes to what the photo reads."""
        from ..agents.expenses.records import LANDED_EVENT, READ_EVENT
        from ..agents.expenses.report_lines import amounts_said
        from ..engine.runner import door_run as run

        said = str(args.get("said") or "")
        found = re.fullmatch(r"events#(\d+)", str(args.get("line") or "").strip())
        rows = (
            t._rows(
                "SELECT event_type, payload_json FROM events WHERE id = ? AND tenant = ?",
                (int(found[1]), t.tenant),
            )
            if found
            else []
        )
        if not rows or rows[0]["event_type"] != LANDED_EVENT:
            return {
                "status": "not_found",
                "say": "That isn't a receipt waiting in a month. Find it in month_report and "
                "pass its source as line.",
            }
        receipt = json.loads(rows[0]["payload_json"] or "{}")
        person = unit_of(str(receipt.get("person", "")))
        if not self._may_place(t.viewer, person):
            return {
                "status": "refused",
                "say": "That receipt isn't theirs, and it isn't in a month they review.",
            }
        sha = str(receipt.get("sha256", ""))
        root = self.tenants_root
        zone = (
            _zone_of(tenant_dir(self.tenant, tenants_root=root), person)
            or load_tenant(self.tenant, tenants_root=root).identity.timezone
        )
        claim = claim_in(said)
        if claim is not None:
            claim = settled(claim, zone)
        else:
            events = [
                {"event_type": READ_EVENT, "payload": json.loads(r["payload_json"] or "{}")}
                for r in t._rows(
                    "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ? "
                    "ORDER BY id",
                    (t.tenant, READ_EVENT),
                )
            ]
            reading = amounts_said(events, READ_EVENT).get(sha)
            claim = from_tag(reading) if reading else None
        if claim is None and (bare := _bare_number(said)):
            return {
                "status": "needs_currency",
                "say": f"They said {bare} with no money named. Ask only whether it was dollars "
                f"or rupees (or the money where they are), keeping the {bare} they said, then "
                "pass both. Never ask how much again.",
            }
        if claim is None:
            return {
                "status": "needs_amount",
                "say": "Ask them how much it was, and in what currency, then pass their "
                "words. Never guess an amount.",
            }
        if not claim.currency:
            return {
                "status": "needs_currency",
                "say": f"Ask them which currency {claim.words()} was in, then pass their words.",
            }
        done = run(
            self.tenant,
            "expenses",
            "amount",
            params={"sha": sha, "tag": claim.tag(), "said": said, "by": self.person},
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=root,
        )
        if done.status == "error":
            return {
                "status": "busy",
                "say": "The books are busy for a moment. Ask them to try again in a minute.",
            }
        day = str(receipt.get("file", ""))[:10]
        return {
            "status": "set",
            "say": f"Done: the {day} receipt is {claim.words()}. Tell them so.",
        }

    # -- statements (t-statements, the owner's decisions of 2026-10-14) ---------

    def bank_statement(self, t: Any, args: dict) -> dict:
        """What waits for the person's word (no statement named); the
        ministry's account kept whole; or the person's own account's lines,
        each with what Tim thinks it looks like, for them to pick."""
        person, name, refused = self._whose(t, args)
        if refused is not None:
            return refused
        held = {h.id: h for h in self.desk.held(person)}
        asked = str(args.get("statement") or "")
        named = self.named(list(held.values()))
        if args.get("as_named") is True and not asked:
            return self._as_named(t, args, [held[k] for k in named], named)
        if not asked:
            return {
                "statements": [
                    {**_held_words(h), **({"named": named[h.id]} if h.id in named else {})}
                    for h in held.values()
                ],
                "say": _whose_words(held, named)
                if held
                else "Nothing they sent is waiting. "
                "Statements go in through their catch-up link (catch_up_link): the CSV file "
                "their bank's website exports.",
            }
        h = held.get(asked)
        if h is None:
            return {
                "status": "not_found",
                "say": "That statement isn't waiting any more (Tim holds one for a day). Ask "
                "them to send it again through their catch-up link.",
            }
        if h.kind == SCHOOL_LIST:
            return {"status": "school_list", "say": "That's a school list: keep_school_list."}
        account = str(args.get("account") or "")
        if h.kind == EXPORT:
            return self._keep_export(t, h, account, str(args.get("service") or ""))
        if account == statements.OWN:
            return self._walk_own(h)
        if account != statements.MINISTRY:
            return {
                "status": "which",
                "say": "Ask them whose account it is: the ministry's own, or their own.",
            }
        done = self._keep(h, statements.MINISTRY, list(h.rows), said="the ministry's account")
        if done is not None:
            return done
        return {
            "status": "kept",
            "lines": len(h.rows),
            "say": f"Kept all {len(h.rows)} lines of the ministry's account, "
            f"{_day_words(h.rows[0].day)} to {_day_words(h.rows[-1].day)}."
            + _closed_out(t, [r.day for r in h.rows])
            + _in_the_bank(t, h.name)
            + " Each shows in its "
            "month (month_report's bank lines); a line that needs its receipt, or what it was "
            "for, says so. Money sent abroad, gifts in, and paying them back are never "
            "spending.",
        }

    def named(self, held: list[Held]) -> dict[str, str]:
        """Each bank statement waiting on whose account it is whose file
        name says so (t-account-from-name): its id to the side the name
        says. One already shown as her own waits on her picks, never on
        this question again."""
        from .statement_names import side_of

        waiting = [h for h in held if h.kind == STATEMENT and h.flagged is None and h.file_name]
        if not waiting:
            return {}
        ministry = load_tenant(self.tenant, tenants_root=self.tenants_root).identity.legal_name
        sides = {h.id: side_of(h.file_name, ministry) for h in waiting}
        return {k: side for k, side in sides.items() if side}

    def _as_named(self, t: Any, args: dict, held: list[Held], named: dict[str, str]) -> dict:
        """Her one yes to the question the names raised: each statement
        whose name said it, settled as its name says. The ministry's is
        kept whole; her own is shown to her for her picks, keeping nothing."""
        if not held:
            return {
                "status": "which",
                "say": "No statement's name says whose account it is. Ask them whose account "
                "each bank statement is: the ministry's own, or their own.",
            }
        answers = []
        for h in held:
            account = named[h.id]
            said = self.bank_statement(
                t, {**args, "as_named": False, "statement": h.id, "account": account}
            )
            answers.append({"statement": h.id, "account": account, **said})
        return {
            "status": "as_named",
            "answers": answers,
            "say": " ".join(str(a.get("say") or "") for a in answers),
        }

    def _keep_export(self, t: Any, h: Held, account: str, named: str) -> dict:
        """A payment service's export (c32): kept whole when it's the
        ministry's own account at the service, never a person's own."""
        from ..agents.expenses import processors
        from ..engine.runner import door_run as run

        service = h.service or " ".join(named.split())[:40]
        if account == statements.OWN:
            return {
                "status": "refused",
                "say": "Tim keeps a payment service's export only for the ministry's own "
                "account, where its gifts come in. Nothing of it was kept.",
            }
        if account != statements.MINISTRY or not service:
            return {
                "status": "which",
                "say": "Ask them whether it's the ministry's own account at the service"
                + ("" if service else ", and which service it is (PayPal, Zeffy, a church app)")
                + ". Then pass account ministry"
                + ("" if service else " and service, as they name it")
                + ".",
            }
        done = run(
            self.tenant,
            "expenses",
            "processor",
            params={
                "person": h.name,
                "service": service,
                "payments": json.dumps([p.payload(h.name, service) for p in h.rows]),
                "by": self.person,
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return _BUSY
        self.desk.drop(h.id)
        gifts = [p for p in h.rows if p.kind == processors.GIFT]
        refunds = [p for p in h.rows if p.kind == processors.REFUND]
        payouts = [p for p in h.rows if p.kind == processors.PAYOUT]
        parts = [f"{_count(len(gifts), 'gift')} (${sum(p.gross for p in gifts) / 100:,.2f})"]
        if refunds:
            parts.append(
                f"{_count(len(refunds), 'refund')} (${-sum(p.gross for p in refunds) / 100:,.2f})"
            )
        if payouts:
            parts.append(f"{_count(len(payouts), 'payout')} to the bank")
        left = processors.left_out_words(h.left_out)
        return {
            "status": "kept",
            "gifts": len(gifts),
            "say": f"Kept {service}'s {_and(parts)}."
            + (f" Left out: {left}." if left else "")
            + _closed_out(t, [p.day for p in h.rows])
            + " Each gift names its donor and shows in its month with the service's fee "
            "as spending; a payout and the bank deposit it became are money moving between "
            "the ministry's own accounts, never income. Donor letters read the gifts.",
        }

    def _walk_own(self, h: Held) -> dict:
        if h.person != self.person:
            return {
                "status": "refused",
                "say": "Someone's own account is theirs alone: only they can go through it. "
                "Nothing of it was shown or kept.",
            }
        listed = list(
            load_tenant(self.tenant, tenants_root=self.tenants_root).expenses.category_accounts
        )
        rows = [
            {
                "row": n,
                "day": ln.day,
                "words": ln.words,
                "dollars": f"{abs(ln.cents) / 100:.2f}",
                "in": ln.cents > 0,
                "looks": statements.looks_like(ln, listed),
            }
            for n, ln in enumerate(h.rows, start=1)
        ]
        self.desk.shown(h.id, tuple(r["row"] for r in rows if r["looks"]))
        return {
            "status": "pick",
            "rows": rows,
            "say": "These are their own account's lines, for them alone. Walk them through the "
            "ones Tim marked (looks: what each looks like) and any others they name, in plain "
            "words: a row number is for your calls, never say it to the person. Then pass the "
            "rows they pick to keep_own_lines with their words; when they say to keep what Tim "
            "flagged, pass flagged true, with the rows of any others they name. Nothing is kept "
            "until they pick, and the rest is never kept.",
        }

    def keep_own(self, t: Any, args: dict) -> dict:
        """The lines the person picked from their own account; the rest are
        dropped and never kept."""
        h = next((x for x in self.desk.held(self.person) if x.id == args.get("statement")), None)
        if h is None or h.kind != STATEMENT:
            return {
                "status": "not_found",
                "say": "That statement isn't waiting any more. Ask them to send it again "
                "through their catch-up link.",
            }
        try:
            named = {int(r) for r in args.get("rows") or []}
        except (TypeError, ValueError):
            named = set()
        flagged = args.get("flagged") is True
        if flagged and h.flagged is None:
            return {
                "status": "which",
                "say": "Tim hasn't shown them this statement's lines yet, so nothing is "
                "flagged. Show them first: bank_statement with its statement and account own.",
            }
        if flagged and not h.flagged and not named:
            return {
                "status": "which",
                "say": "Tim flagged none of this statement's lines. Ask them which lines were "
                "the ministry's, then pass those rows.",
            }
        # "Keep what you flagged" (t-keep-flagged): only the lines Tim showed
        # her as flagged on this statement, never a line she wasn't shown.
        picks = sorted(named | (set(h.flagged or ()) if flagged else set()))
        if not picks or picks[0] < 1 or picks[-1] > len(h.rows):
            return {"status": "which", "say": "Pass the rows they picked, from bank_statement."}
        picked = [h.rows[n - 1] for n in picks]
        # A line already named someone's pay is that pay, never a payback (#653).
        from ..agents.expenses.records import LINE_PURPOSE_EVENT

        said = {str(e.get("sha256")): e for e in self._payloads(t, LINE_PURPOSE_EVENT)}
        ministry = [
            statements.Line.of(e)
            for e in self._kept_lines(t)
            if e.get("person") == h.name
            and e.get("account") == statements.MINISTRY
            and not said.get(str(e.get("sha256")), {}).get("pay_of")
        ]
        moved = [
            m.sha(h.name, statements.MINISTRY)
            for m, _o in statements.match_transfers(ministry, list(h.rows))
        ]
        done = self._keep(
            h, statements.OWN, picked, said=str(args.get("said") or ""), transfers=moved
        )
        if done is not None:
            return done
        rest = len(h.rows) - len(picked)
        paid = (
            f" {len(moved)} payment{'s' if len(moved) != 1 else ''} from the ministry's account "
            "landed in theirs: paid back, never spending."
            if moved
            else ""
        )
        return {
            "status": "kept",
            "lines": len(picked),
            "say": f"Kept the {len(picked)} line{'s' if len(picked) != 1 else ''} they picked. "
            f"The other {rest} were never kept and are gone from Tim.{paid}"
            + _closed_out(t, [r.day for r in picked])
            + " Tell them so.",
        }

    def keep_school(self, t: Any, args: dict) -> dict:
        """The person's school list: each payment a line with no receipt."""
        from ..engine.runner import door_run as run

        person, _name, refused = self._whose(t, args)
        if refused is not None:
            return refused
        h = next((x for x in self.desk.held(person) if x.id == args.get("statement")), None)
        if h is None or h.kind != SCHOOL_LIST:
            return {
                "status": "not_found",
                "say": "That school list isn't waiting any more. Ask them to send it again "
                "through their catch-up link.",
            }
        rows = [
            {
                "day": r.day,
                "student": r.student,
                "school": r.school,
                "amount": str(r.amount),
                "currency": r.currency,
                **({"paid_by": r.paid_by} if r.paid_by else {}),
            }
            for r in h.rows
        ]
        done = run(
            self.tenant,
            "expenses",
            "school",
            params={
                "person": h.name,
                "rows": json.dumps(rows),
                "by": self.person,
                "said": str(args.get("said") or ""),
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return _BUSY
        self.desk.drop(h.id)
        return {
            "status": "kept",
            "payments": len(rows),
            "say": f"Added {len(rows)} school payment{'s' if len(rows) != 1 else ''}, each in "
            "its own month with no receipt needed. One in another currency counts at its "
            "day's rate." + _closed_out(t, [r.day for r in h.rows]),
        }

    def _pays_back(self, t: Any, name: str) -> str | None:
        """Whom a line paid back: the person an approved expense of their own
        names (c28), "" when nothing approved is owed to anyone by that name,
        None when the line names nobody."""
        from ..agents.expenses import advances

        if not name:
            return None
        owed = {
            str(c.get("name") or c["person"])
            for c in advances.approved(
                t._rows(advances.APPROVED, (t.tenant, advances.ADVANCE_CARD))
            )
        }
        return next((p for p in owed if p.casefold() == name.casefold()), "")

    def _policy(self, t: Any) -> Any:
        """The ministry's authority policy: the viewer's, or read from disk."""
        from ..engine.authority_gate import load_policy

        if t.viewer is not None:
            return t.viewer.policy
        return load_policy(self.tenant, tenants_root=self.tenants_root).policy

    def _gift_of(
        self, t: Any, args: dict, line: dict, own: bool, theirs_in: bool
    ) -> tuple[str, bool, str, str] | dict:
        """Who gave a deposit, whether it was a grant from a donor-advised
        fund, and whose fund it was given toward, as (giver, grant, their id,
        their name) (c32, c38, c34 PR 3); what to say back when it can't be."""
        giver = " ".join(str(args.get("gift_from") or "").split())
        grant = bool(args.get("fund_grant")) and bool(giver) and not own
        money_in = (
            line.get("account") == statements.MINISTRY
            and statements.Line.of(line).kind == statements.IN
        )
        if giver and ((own and not theirs_in) or not money_in):
            return _NO_DONOR  # a gift for them on their own money in names its giver (c38)
        named = " ".join(str(args.get("for_fund") or "").split())
        if not named:
            return giver, grant, "", ""
        if not giver or own or not money_in:
            return {
                "status": "asks",
                "say": "Only a gift to the ministry, on its own account and naming who gave it "
                "in gift_from, can count toward someone's fund.",
            }
        policy = self._policy(t)
        who = policy.person_named(named)
        if who is None:
            return {
                "status": "asks",
                "say": f"Tim knows no one in the ministry named {named}. A fund is for someone "
                "the ministry supports: ask whom the gift was given toward, by their full name.",
            }
        wall = _fund_wall(policy, who, giver, bool(args.get("not_family")))
        return wall if wall is not None else (giver, grant, who, policy.display(who))

    def _pay_of(self, t: Any, name: str, line: dict) -> tuple[str, str, Any] | dict | None:
        """Whose pay a line was (c34): (their id, their name, the month's pay)
        where the board set pay for them in the line's month; what to ask
        when it can't be; None when the line names nobody's pay."""
        from ..agents.policy.jobs import pay_in_force
        from .catalog import _rulebook

        if not name:
            return None
        if (
            line.get("account") != statements.MINISTRY
            or statements.Line.of(line).kind != statements.OUT
        ):
            return {
                "status": "asks",
                "say": "Only a payment out of the ministry's own account can be someone's pay.",
            }
        policy = self._policy(t)
        who = policy.person_named(name)
        month = str(line["day"])[:7]
        pay = None
        if who:
            con = t._db()
            try:
                pay = pay_in_force(con, t.tenant, _rulebook(t), who, month)
            finally:
                con.close()
        shown = policy.display(who) if who else name
        if pay is None:
            when = date.fromisoformat(month + "-01").strftime("%B %Y")
            return {
                "status": "asks",
                "say": f"The board set no pay for {shown} in {when}. A line is someone's pay "
                "only in a month the board's pay resolution covers (set_pay).",
            }
        return who, shown, pay

    def _paid_whom(
        self, t: Any, args: dict, line: dict, kind: str
    ) -> tuple[str | None, tuple[str, str, Any] | None] | dict:
        """Whom a line out paid: someone paid back (c28) and someone's pay
        (c34), each None when it names nobody; what to ask when it can't be."""
        pays = self._pays_back(t, str(args.get("pays_back") or "").strip())
        if pays is not None and (
            line.get("account") != statements.MINISTRY or kind != statements.OUT
        ):
            return {
                "status": "asks",
                "say": "Only a payment out of the ministry's own account can pay someone back.",
            }
        if pays == "":
            return {
                "status": "asks",
                "say": "Tim knows of nothing approved that the ministry owes them. An expense they "
                "paid themselves is recorded first (paid_it_myself) and approved by someone else.",
            }
        pay_of = self._pay_of(t, str(args.get("pay_of") or "").strip(), line)
        return pay_of if isinstance(pay_of, dict) else (pays, pay_of)

    def explain_line(self, t: Any, args: dict) -> dict:
        """What one statement line was for, in the person's words."""
        from ..engine.runner import door_run as run

        said = str(args.get("said") or "").strip()
        found = re.fullmatch(r"bank#([0-9a-f]{16})", str(args.get("line") or "").strip())
        line = next(
            (e for e in self._kept_lines(t) if found and str(e["sha256"]).startswith(found[1])),
            None,
        )
        if line is None or not said:
            return {
                "status": "not_found",
                "say": "That isn't a bank line. Find it in month_report's bank lines and pass "
                "its source as line, with their words in said.",
            }
        if not self._may_place(t.viewer, unit_of(str(line.get("person", "")))):
            return {
                "status": "refused",
                "say": "That line isn't theirs, and it isn't in a month they review.",
            }
        listed = list(
            load_tenant(self.tenant, tenants_root=self.tenants_root).expenses.category_accounts
        )
        atm = statements.Line.of(line).kind == statements.ATM
        claim = claim_in(said)
        if atm:
            root = self.tenants_root
            zone = (
                _zone_of(tenant_dir(self.tenant, tenants_root=root), unit_of(str(line["person"])))
                or load_tenant(self.tenant, tenants_root=root).identity.timezone
            )
            # Asked what the slip said, "Feb 14 gave 26,500" means rupees in
            # Kathmandu (the dress rehearsal, 2026-10-15).
            local, bare = local_currency(zone), slip_amount(said)
            if claim is None and local and bare:
                claim = Claim(bare.quantize(Decimal("0.01")), local, local)
            claim = settled(claim, zone) if claim is not None else None
        gave = claim.tag() if atm and claim and claim.currency not in ("", "USD") else ""
        if atm and not gave:
            return {
                "status": "asks",
                "say": "That's cash from an ATM. Ask how many rupees (or whichever money) it "
                "gave; the ATM slip shows it. Then pass their words again.",
            }
        # Their own on the ministry's account: owed to the ministry, never
        # spending (Caleb's first night, 2026-10-25).
        own = str(args.get("whose") or "").strip() == statements.OWN
        kind = statements.Line.of(line).kind
        moves_out = kind in (statements.OUT, statements.SEND)  # a send home too (2026-10-29)
        # Money in that was theirs (their pay landing here, a gift for them)
        # goes to their fund (c31, the EquipNet lesson, 2026-11-12).
        theirs_in = own and kind == statements.IN
        from_pay = str(args.get("whose") or "").strip() == statements.PAY
        refused = _whose_refused(line, own and (moves_out or theirs_in), own, from_pay)
        if refused is not None:
            return refused
        paid = self._paid_whom(t, args, line, kind)
        if isinstance(paid, dict):
            return paid
        pays, pay_of = paid
        gift = self._gift_of(t, args, line, own, theirs_in)
        if isinstance(gift, dict):
            return gift
        giver, grant, fund, fund_name = gift
        # Money in never carries a spending category (take 5, M01: "deposited it
        # by phone" filed Aunt Joan's gift under Communication).
        asked = "" if kind == statements.IN else str(args.get("category") or "").strip()
        category = canonical(asked, listed) if asked else ""
        if category is None:
            return {
                "status": "needs_category",
                "say": f"The ministry's categories are {', '.join(listed)}. Ask which one "
                "fits, then pass it as category.",
            }
        if not asked and not atm and not own and not pays and not pay_of and kind != statements.IN:
            category = from_words(said, listed)
        done = run(
            self.tenant,
            "expenses",
            "line_purpose",
            params={
                "sha": str(line["sha256"]),
                "said": said,
                "category": "" if own or pays or pay_of else category,
                "by": self.person,
                **({"cash": gave} if gave else {}),
                **({"whose": statements.OWN} if own else {}),
                **({"whose": statements.PAY} if from_pay else {}),
                **({"pays_back": pays} if pays else {}),
                **({"pay_of": pay_of[0]} if pay_of else {}),
                **_gift_params(giver, grant, fund),
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return _BUSY
        if pay_of:
            return _pay_done(line, *pay_of[1:])
        whose_done = _whose_done(line, pays, theirs_in, own, giver) or _from_pay_done(
            line, from_pay, category
        )
        if whose_done is not None:
            return whose_done
        if gave:
            dollars = Decimal(-int(line["cents"])) / 100
            each = (claim.amount / dollars).quantize(Decimal("0.01")) if dollars else Decimal(0)
            return {
                "status": "set",
                "say": f"Done: the {_day_words(str(line['day']))} cash was {claim.words()} for "
                f"${dollars:,.2f}, so {each:,.2f} to the dollar. Receipts paid from that cash "
                "count at what it cost.",
            }
        day = _day_words(str(line["day"]))
        if giver:
            return _gift_done(day, int(line["cents"]), giver, grant, category, fund_name)
        if category:
            return {
                "status": "set",
                "category": category,
                "say": f"Done: the {day} line is in, as they said it. It's under {category}; "
                "tell them so.",
            }
        if kind != statements.OUT:
            return {"status": "set", "category": "", "say": f"Done: the {day} line is in."}
        return {
            "status": "set",
            "category": "",
            "say": f"Done: the {day} line is in, as they said it, with no category yet. The "
            f"ministry's categories are {', '.join(listed)}. Propose the one that fits their "
            "words, say which, and once they agree pass it as category with the same line.",
        }

    # -- the person's cash (t-cash-count, t-split-receipt, 2026-10-15) ----------

    def _kept(self, t: Any, claim: Claim, days: set[str]) -> tuple[str, str] | None:
        """(day, source) of a receipt of theirs Tim already keeps on a day
        their words name, at the same amount in the same money (persona night
        S08, 2026-11-16), else None. With no day named it is a new receipt:
        the same taxi fare recurs."""
        from ..agents.expenses.records import LANDED_EVENT

        if not days:
            return None

        rows = t._rows(
            "SELECT id, event_type, payload_json FROM events WHERE tenant = ? ORDER BY id",
            (t.tenant,),
        )
        log = [
            {"event_type": r["event_type"], "payload": json.loads(r["payload_json"] or "{}")}
            for r in rows
        ]
        landed = {
            str(e["payload"].get("sha256")): r["id"]
            for r, e in zip(rows, log, strict=True)
            if e["event_type"] == LANDED_EVENT
        }
        for sha, day, tag, person in statements.kept_receipts(log):
            if (
                day[5:] in days
                and unit_of(person) == self.person
                and statements.amount_of(tag)
                == (
                    claim.currency,
                    claim.amount,
                )
            ):
                return day, f"events#{landed[sha]}"
        return None

    def _cash_log(self, t: Any) -> list[dict]:
        return [
            {"event_type": r["event_type"], "payload": json.loads(r["payload_json"] or "{}")}
            for r in t._rows(
                "SELECT event_type, payload_json FROM events WHERE tenant = ? ORDER BY id",
                (t.tenant,),
            )
        ]

    def _zone(self) -> str:
        return person_zone(self.tenant, self.tenants_root, self.person)

    def _cash_today(self, t: Any) -> str:
        return str(t.today) if t.today else local_today(self._zone())

    def _cash_claim(self, said: str, currencies: list[str]) -> Claim | None:
        """The amount in their words, its currency settled by where they are,
        else by the one currency of cash they hold."""
        claim = claim_in(said)
        if claim is None:
            return None
        claim = settled(claim, self._zone())
        if not claim.currency and len(currencies) == 1:
            claim = Claim(claim.amount, currencies[0], claim.unit)
        return claim

    def _run_cash(self, params: dict) -> bool:
        from ..engine.runner import door_run as run

        done = run(
            self.tenant,
            "expenses",
            "cash",
            params={**params, "by": self.person},
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        return done.status != "error"

    def count_cash(self, t: Any, args: dict) -> dict:
        """What the person says is in hand, held against what the books say
        they hold: the same, more, or less, and what Tim asks next."""
        if not self.offered():
            return {"status": "refused", "say": "Only someone who files expenses counts cash."}
        log = self._cash_log(t)
        day = self._cash_today(t)
        _drawn, on_hand = statements.cash(log, through=day)
        held = {cur: v for (who, cur), v in on_hand.items() if who == self.name}
        return self._counted(log, day, held, args)

    def _counted(self, log: list[dict], day: str, held: dict, args: dict) -> dict:
        claim = self._cash_claim(str(args.get("said") or ""), sorted(held))
        if claim is None or not claim.currency or claim.currency == "USD":
            return {
                "status": "asks",
                "say": "Ask how much cash they have in hand right now, and in what money "
                "(rupees, for example), then pass their words.",
            }
        books = held.get(claim.currency, (Decimal(0), 0))[0]
        sha = hashlib.sha256(f"count:{self.name}|{day}|{len(statements.counts(log))}".encode())
        params = {
            "what": "count",
            "sha": sha.hexdigest(),
            "person": self.name,
            "day": day,
            "tag": claim.tag(),
            "said": str(args.get("said") or ""),
        }
        if not self._run_cash(params):
            return _BUSY
        gap = books - claim.amount
        have = statements.cash_words(f"{claim.currency}{books}")
        if not gap:
            return {"status": "matches", "say": f"Their {claim.words()} matches the books."}
        if gap > 0:
            short = statements.cash_words(f"{claim.currency}{gap}")
            return {
                "status": "gap",
                "gap": f"{claim.currency}{gap:.2f}",
                "say": f"The books have {have} in hand, so {short} went with no receipt. "
                "Ask what it went to: the ministry's spending (tea, taxis, tips: a few words "
                "are enough), or their own use, or some of each. Pass their words to "
                "explain_cash, once for each part.",
            }
        more = statements.cash_words(f"{claim.currency}{-gap}")
        return {
            "status": "more",
            "say": f"They have {more} more than the books ({have}). A receipt may be in "
            "twice or for too much, or some of their own money is in with the ministry's. "
            "Ask which; nothing is changed until they say.",
        }

    def split_line(self, t: Any, args: dict) -> dict:
        """An amount out of one line, in the person's words (the owner,
        2026-11-10: "1,300 of that was mine", "5,000 of that was
        scholarships"): a bank line, cash gone with no receipt, or a receipt
        that was partly theirs. The rest stays where it was. Never percent."""
        from ..engine.runner import door_run as run

        ref = str(args.get("line") or "").strip()
        said = str(args.get("said") or "").strip()
        whose = statements.OWN if args.get("whose") == "own" else statements.MINISTRY
        if ref.startswith("events#"):
            if whose != statements.OWN:
                return {
                    "status": "asks",
                    "say": "A receipt comes apart only into what was theirs and what was the "
                    "ministry's. Ask how much of it was theirs, then pass their words with whose "
                    "as own.",
                }
            return self.split_receipt(t, {"line": ref, "said": said, "part_is": "own"})
        found = re.fullmatch(r"bank#([0-9a-f]{16})", ref)
        log = self._cash_log(t)
        lines = [*self._kept_lines(t), *statements.used(log)]
        line = next(
            (e for e in lines if found and str(e["sha256"]).startswith(found[1])),
            None,
        )
        if line is None or not said:
            return {
                "status": "not_found",
                "say": "That isn't a line in a month. Find it in month_report and pass its "
                "source as line, with their words in said.",
            }
        if not self._may_place(t.viewer, unit_of(str(line.get("person", "")))):
            return {
                "status": "refused",
                "say": "That line isn't theirs, and it isn't in a month they review.",
            }
        sha = str(line["sha256"])
        money = statements.amount_of(str(line.get("tag") or "")) or (
            "USD",
            Decimal(abs(statements.Line.of(line).cents)) / 100,
        )
        currency, whole = money
        amount = None if "%" in said else _amount_said(said, currency)
        if amount is None:
            return {
                "status": "asks",
                "say": f"Ask how much of the {_money_words(currency, whole)} it was, as an "
                "amount in the line's money (never a percent), then pass their words.",
            }
        key = statements.OWN if whose == statements.OWN else ""
        listed = list(
            load_tenant(self.tenant, tenants_root=self.tenants_root).expenses.category_accounts
        )
        category = ""
        if whose == statements.MINISTRY:
            asked = str(args.get("category") or "").strip()
            category = (canonical(asked, listed) if asked else from_words(said, listed)) or ""
            if not category:
                return {
                    "status": "needs_category",
                    "say": f"The ministry's categories are {', '.join(listed)}. Ask which one "
                    "that part was, then pass it as category.",
                }
            key = category.casefold()
        split = statements.line_splits(log).get(sha)
        cents = round(amount * 100)
        left = round(whole * 100) - sum(
            statements.take_cents(x, currency) or 0 for x in statements.taken(split, but=key)
        )
        # The last part may take all that's left when old percents still hold
        # it: else they'd divide it (Caleb's October cash, said again by
        # amount, 2026-11-12).
        last = split is not None and bool(split.parts) and cents == left
        if not (0 < cents < left or last):
            return {
                "status": "asks",
                "say": f"The line is {_money_words(currency, whole)}, and "
                f"{_money_words(currency, Decimal(left) / 100)} of it is still where it was. "
                "A part has to be less than that. If the whole line was one thing, say what it "
                "was for instead (explain_bank_line). Otherwise ask again.",
            }
        tag = f"${amount:.2f}" if currency == "USD" else f"{currency}{amount:.2f}"
        done = run(
            self.tenant,
            "expenses",
            "line_split",
            params={
                "sha": sha,
                "said": said,
                "by": self.person,
                "take": tag,
                "category": category,
                "whose": whose,
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return _BUSY
        part = _money_words(currency, amount)
        rest = _money_words(currency, Decimal(left - cents) / 100)
        went = (
            "was theirs (owed to the ministry when the ministry's money paid it)"
            if whose == statements.OWN
            else f"goes under {category}"
        )
        return {
            "status": "set",
            "say": f"Done: {part} of the {_day_words(str(line['day']))} line {went}; the other "
            f"{rest} stays where it was. Tell them so.",
        }

    def explain_cash(self, t: Any, args: dict) -> dict:
        """Where cash with no receipt went, against their last count: the
        ministry's spending or their own use, the amount from their words or
        the whole gap still open."""
        if not self.offered():
            return {"status": "refused", "say": "Only someone who files expenses counts cash."}
        log = self._cash_log(t)
        mine = [c for c in statements.counts(log) if c.get("person") == self.name]
        if not mine:
            return {
                "status": "asks",
                "say": "Count first: ask how much cash they have in hand and pass it to "
                "count_cash.",
            }
        last = mine[-1]
        currency, counted = statements.amount_of(str(last["tag"])) or ("", Decimal(0))
        _drawn, on_hand = statements.cash(log, through=str(last["day"]))
        open_gap = on_hand.get((self.name, currency), (Decimal(0), 0))[0] - counted
        if open_gap <= 0:
            return {"status": "settled", "say": "Their last count matches the books already."}
        said = str(args.get("said") or "").strip()
        claim = self._cash_claim(said, [currency])
        if claim is None and re.search(r"\d", said):  # "2,000 went to my family": 2,000 what?
            return {
                "status": "asks",
                "say": "Their words have a number but no money. Ask what it was in (rupees, "
                "for example), then pass their words again.",
            }
        amount = claim.amount if claim and claim.currency == currency else open_gap
        if amount <= 0 or amount > open_gap:
            gap = statements.cash_words(f"{currency}{open_gap}")
            return {
                "status": "asks",
                "say": f"Only {gap} is still open from their count. Ask again.",
            }
        whose = "own" if str(args.get("whose") or "") == "own" else "ministry"
        told = (self.name, str(last["day"]), f"{currency}{amount:.2f}", whose, said)
        if said and any(
            tuple(str(u.get(k) or "") for k in ("person", "day", "tag", "whose", "said")) == told
            for u in statements.used(log)
        ):
            # The same words again are a retry, never a second spend.
            gap = statements.cash_words(f"{currency}{open_gap}")
            return {
                "status": "already",
                "say": f"That is already in the books, once. {gap} is still open. If it "
                "happened a second time, ask them to say so.",
            }
        listed = list(
            load_tenant(self.tenant, tenants_root=self.tenants_root).expenses.category_accounts
        )
        asked = str(args.get("category") or "").strip()
        category = canonical(asked, listed) if asked else from_words(said, listed)
        if category is None:
            return {
                "status": "needs_category",
                "say": f"The ministry's categories are {', '.join(listed)}. Ask which one "
                "fits, then pass it as category.",
            }
        n = len(statements.used(log))
        params = {
            "what": "used",
            "sha": hashlib.sha256(f"used:{last['sha256']}|{n}".encode()).hexdigest(),
            "person": self.name,
            "day": str(last["day"]),
            "tag": f"{currency}{amount:.2f}",
            "whose": whose,
            "said": said,
            "category": category if whose == "ministry" else "",
        }
        if not self._run_cash(params):
            return _BUSY
        words = statements.cash_words(f"{currency}{amount}")
        left = open_gap - amount
        rest = (
            f" {statements.cash_words(f'{currency}{left}')} is still open."
            if left
            else " The count matches the books now."
        )
        if whose == "own":
            return {
                "status": "set",
                "say": f"Done: {words} went to their own use. If it was the ministry's cash, "
                f"its month shows what they owe the ministry.{rest}",
            }
        if category:
            return {
                "status": "set",
                "category": category,
                "say": f"Done: {words} of ministry spending, under {category}; tell them so.{rest}",
            }
        return {
            "status": "set",
            "category": "",
            "say": f"Done: {words} of ministry spending, as they said, with no category yet. "
            f"The ministry's categories are {', '.join(listed)}. If the cash went to several "
            "kinds of things, say each part again with its amount (one call each). Otherwise "
            f"propose the one that fits, and once they agree, pass it as category.{rest}",
        }

    def split_receipt(self, t: Any, args: dict) -> dict:
        """A receipt that was partly their own: the ministry's part stays,
        and their part is never counted (owed to the ministry when ministry
        money paid it)."""
        from ..agents.expenses.records import LANDED_EVENT
        from ..agents.expenses.report_lines import amounts_said

        found = re.fullmatch(r"events#(\d+)", str(args.get("line") or "").strip())
        rows = (
            t._rows(
                "SELECT event_type, payload_json FROM events WHERE id = ? AND tenant = ?",
                (int(found[1]), t.tenant),
            )
            if found
            else []
        )
        if not rows or rows[0]["event_type"] != LANDED_EVENT:
            return {
                "status": "not_found",
                "say": "That isn't a receipt in a month. Find it in month_report and pass "
                "its source as line.",
            }
        receipt = json.loads(rows[0]["payload_json"] or "{}")
        if not self._may_place(t.viewer, unit_of(str(receipt.get("person", "")))):
            return {"status": "refused", "say": "That receipt isn't theirs."}
        sha = str(receipt.get("sha256", ""))
        log = self._cash_log(t)
        whole = amounts_said(log).get(sha) or ""
        own = statements.splits(log).get(sha, "")
        whole = statements.plus_tag(whole, own)  # split again: from the whole receipt
        got = statements.amount_of(whole)
        if got is None:
            return {
                "status": "asks",
                "say": "Set the receipt's whole amount first (set_amount), then split it.",
            }
        currency, total = got
        claim = self._cash_claim(str(args.get("said") or ""), [currency])
        if claim is None or (claim.currency or currency) != currency:
            return {
                "status": "asks",
                "say": f"Ask how much of the {statements.cash_words(whole) or whole} was "
                "theirs (or the ministry's), in the receipt's money, then pass their words.",
            }
        part = claim.amount if args.get("part_is") != "own" else total - claim.amount
        theirs = total - part
        if part <= 0 or theirs <= 0:
            return {"status": "asks", "say": "That's all of it or none of it. Ask again."}

        def tag(amount: Decimal) -> str:
            return f"${amount:.2f}" if currency == "USD" else f"{currency}{amount:.2f}"

        params = {
            "what": "split",
            "sha": sha,
            "tag": tag(theirs),
            "part": tag(part),
            "said": str(args.get("said") or ""),
        }
        if not self._run_cash(params):
            return _BUSY
        return {
            "status": "set",
            "say": f"Done: {tag(part)} of it was the ministry's and {tag(theirs)} theirs. "
            "Only the ministry's part counts; when ministry money paid for it, their part "
            "shows as owed to the ministry.",
        }

    def pick_card_receipt(self, t: Any, args: dict) -> dict:
        """Which of the receipts Tim asked about a card line carried, in the
        person's words (t-ask-which-receipt): only one Tim asked about."""
        found = re.fullmatch(r"bank#([0-9a-f]{16})", str(args.get("line") or "").strip())
        line = next(
            (e for e in self._kept_lines(t) if found and str(e["sha256"]).startswith(found[1])),
            None,
        )
        if line is None:
            return {
                "status": "not_found",
                "say": "That isn't a bank line. Find the line that waits on which receipt it "
                "paid in month_report and pass its source as line.",
            }
        if not self._may_place(t.viewer, unit_of(str(line.get("person", "")))):
            return {
                "status": "refused",
                "say": "That line isn't theirs, and it isn't in a month they review.",
            }
        sha = str(line["sha256"])
        asked = statements.in_doubt(self._cash_log(t)).get(sha, [])
        named = re.fullmatch(r"events#(\d+)", str(args.get("receipt") or "").strip())
        rows = (
            t._rows(
                "SELECT payload_json FROM events WHERE id = ? AND tenant = ?",
                (int(named[1]), t.tenant),
            )
            if named
            else []
        )
        receipt = str(json.loads(rows[0]["payload_json"] or "{}").get("sha256", "")) if rows else ""
        if receipt not in asked:
            return {
                "status": "asks",
                "say": "Pass one of the receipts in that line's could_be, the one they say "
                "they paid by card.",
            }
        params = {"what": "rode", "sha": receipt, "line": sha, "said": str(args.get("said") or "")}
        if not self._run_cash(params):
            return _BUSY
        return {
            "status": "set",
            "say": "Done: that receipt rides the card line, at the card's dollars. The other "
            "counts on its own, from their cash or at its day's rate.",
        }

    def _kept_lines(self, t: Any) -> list[dict]:
        return self._payloads(t, statements.LINE_EVENT)

    def _payloads(self, t: Any, event_type: str) -> list[dict]:
        """Every event of one type in this ministry's ledger, oldest first."""
        return [
            json.loads(r["payload_json"] or "{}")
            for r in t._rows(
                "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ? ORDER BY id",
                (t.tenant, event_type),
            )
        ]

    def _keep(
        self,
        h: Held,
        account: str,
        lines: list,
        *,
        said: str,
        transfers: list[str] | None = None,
    ) -> dict | None:
        """Lines to the books, then the held file forgotten; None when done."""
        from ..engine.runner import door_run as run

        listed = list(
            load_tenant(self.tenant, tenants_root=self.tenants_root).expenses.category_accounts
        )
        rows = [
            {
                "day": ln.day,
                "words": ln.words,
                "cents": ln.cents,
                "n": ln.n,
                "looks": statements.looks_like(ln, listed),
                # The bank's balance rides the ministry's account alone (balances.py).
                **(
                    {"day_end": ln.day_end}
                    if account == statements.MINISTRY and ln.day_end is not None
                    else {}
                ),
            }
            for ln in lines
        ]
        done = run(
            self.tenant,
            "expenses",
            "statement",
            params={
                "person": h.name,
                "account": account,
                "lines": json.dumps(rows),
                "transfers": json.dumps(transfers or []),
                "by": self.person,
                "said": said,
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return _BUSY
        self.desk.drop(h.id)
        return None


def _in_the_bank(t: Any, name: str) -> str:
    """The bank's balance against the books, once a statement gave one."""
    from ..agents.expenses import balances

    log = [
        {"event_type": r["event_type"], "payload": json.loads(r["payload_json"] or "{}")}
        for r in t._rows(
            "SELECT event_type, payload_json FROM events WHERE tenant = ? "
            "AND event_type IN (?, ?) ORDER BY id",
            (t.tenant, statements.LINE_EVENT, balances.BALANCE_EVENT),
        )
    ]
    mine = [s for s in balances.standing(log, through="9999-12-31") if s.person == name]
    return f" {balances.words(mine[0])}" if mine else ""


_BUSY = {"status": "busy", "say": "The books are busy for a moment. Try again in a minute."}


def _closed_out(t: Any, days: list[str]) -> str:
    """The lines a closed year kept out, as the person hears it (the owner,
    2026-11-10: the books close once a year)."""
    from ..agents.expenses import year_close

    closed = year_close.from_cards(t._rows(year_close.APPROVED, year_close.marks(t.tenant)))
    return year_close.left_out(days, closed)


_day_words = statements.day_words


def _whose_words(held: dict[str, Held], named: dict[str, str]) -> str:
    """What the AI hears of statements waiting on whose account they are:
    the one question when their names say it (t-account-from-name), else
    the question for each."""
    from .statement_names import ask

    head = "Each statement's id is for your calls; never say it to the person. "
    rest = "A school list goes in with keep_school_list."
    each = (
        "Ask them whose account each bank statement is (the ministry's, or their own), then "
        "call bank_statement with its statement and account. "
    )
    sides = list(named.values())
    question = ask(sides.count(statements.MINISTRY), sides.count(statements.OWN))
    if not question:
        return head + each + rest
    others = any(h.kind == STATEMENT and h.id not in named for h in held.values())
    return (
        head
        + "Their file names say whose account "
        + ("each is (named)" if len(named) > 1 else "it is (named)")
        + f', so ask only this, in these words: "{question}" On their yes, call '
        "bank_statement with as_named true, once: it settles every statement as its name "
        "says. On a no, ask them whose account each bank statement is (the ministry's, or "
        "their own), then call bank_statement with its statement and account. "
        + ("For the others, with no name that says, ask whose account each is. " if others else "")
        + rest
    )


def _held_words(h: Held) -> dict:
    return {
        "statement": h.id,
        "kind": h.kind,
        "lines": len(h.rows),
        "from": h.rows[0].day,
        "to": h.rows[-1].day,
        **({"service": h.service} if h.kind == EXPORT else {}),
    }


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _and(parts: list[str]) -> str:
    if len(parts) < 3:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _export_words(h: Held) -> str:
    """What the person hears when a service's export arrives."""
    from ..agents.expenses import processors

    kinds = [p.kind for p in h.rows]
    parts = [
        _count(kinds.count(k), word)
        for k, word in (
            (processors.GIFT, "gift"),
            (processors.REFUND, "refund"),
            (processors.PAYOUT, "payout"),
        )
        if k in kinds
    ]
    first, last = date.fromisoformat(h.rows[0].day), date.fromisoformat(h.rows[-1].day)
    service = h.service or "payment service"
    ask = (
        f"whether it's the ministry's own {h.service} account."
        if h.service
        else "which service it's from, and whether it's the ministry's own account there."
    )
    return (
        f"Got your {service} export: {_and(parts)}, {first:%b} {first.day} to {last:%b} "
        f"{last.day}. Tell Tim {ask}"
    )


def _day_of(said: Any, today: date) -> date | None:
    """A YYYY-MM-DD day that has come, or None."""
    try:
        day = datetime.strptime(str(said or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return day if day <= today else None


def record_read(
    tenant: str,
    tenants_root: Path | None,
    ledger_root: str | Path,
    *,
    sha: str,
    claim: Claim | None,
    day: date | None,
    payee: str = "",
    category: str = "",
    by: str = "",
) -> str:
    """What a receipt's photo reads, recorded as a proposal and never as
    anyone's word: the amount (with who it reads as paid, and for what)
    waits for the person's yes; the day files it in its month. The person's
    own AI does this through record_reading, and the receipt page when Tim
    reads the photo himself (card R1). The date run's status, or "" when
    there was no day to file."""
    from ..engine.runner import door_run as run

    ledger = Path(ledger_root).parent
    params = {"sha": sha, "read": "photo"}
    for key, said in (("payee", payee), ("category", category)):
        said = said.strip()[:120]
        if said:
            params[key] = said
    if claim is not None:
        run(
            tenant,
            "expenses",
            "amount",
            params={**params, "tag": claim.tag()},
            ledger_dir=ledger,
            tenants_root=tenants_root,
        )
    if day is None:
        return ""
    done = run(
        tenant,
        "expenses",
        "date",
        params={"sha": sha, "day": day.isoformat(), "read": "photo", "by": by},
        ledger_dir=ledger,
        tenants_root=tenants_root,
    )
    return done.status


def agree_read(
    tenant: str,
    tenants_root: Path | None,
    ledger_root: str | Path,
    *,
    sha: str,
    tag: str,
    category: str,
    said: str,
    by: str,
) -> int:
    """The person's yes to one reading: the amount as read (``tag``) becomes
    their word, and a category read (when nobody placed the receipt yet)
    places it. Leave either empty to skip it. 1 when an amount was set."""
    from ..engine.runner import door_run as run

    ledger = Path(ledger_root).parent
    if tag:
        run(
            tenant,
            "expenses",
            "amount",
            params={"sha": sha, "tag": tag, "said": said, "by": by},
            ledger_dir=ledger,
            tenants_root=tenants_root,
        )
    if category:
        run(
            tenant,
            "expenses",
            "categorize",
            params={"sha": sha, "category": category, "by": by, "said": said},
            ledger_dir=ledger,
            tenants_root=tenants_root,
        )
    return 1 if tag else 0


def from_tag(tag: str) -> Claim:
    """A claim back from its tag ("$43.00", "NPR4300.00")."""
    if tag.startswith("$"):
        return Claim(Decimal(tag[1:]), "USD", "$")
    return Claim(Decimal(tag[3:]), tag[:3], tag[:3])


def reader_for(name: str) -> Any:
    """The ministry's photo reader ([expenses] reader), or None."""
    if name == "pretend":
        from ..adapters.pretend_reader import PretendReader

        return PretendReader()
    return None


def filer_name(tenant: str, tenants_root: Path | None, person: str) -> str:
    """The name ``person`` files expenses under in the drop tree, or "" for
    someone who files none here (add_receipt and setup offer nothing)."""
    cfg = load_tenant(tenant, tenants_root=tenants_root)
    if not cfg.expenses.drop_dir:
        return ""
    return next((p.name for p in cfg.expenses.persons if unit_of(p.name) == person), "")


def person_entry(directory: Path, person: str) -> dict:
    """What authority.toml says beside ``person`` (their address, their
    zone), read as plain data; empty when there is nothing."""
    try:
        people = tomllib.loads((Path(directory) / "authority.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    entry = people.get("people", {}).get(person, {})
    return entry if isinstance(entry, dict) else {}


def person_zone(tenant: str, tenants_root: str | Path | None, person: str) -> str:
    """Her own clock: the zone onboarding wrote beside her, else the
    ministry's."""
    root = Path(tenants_root) if tenants_root else None
    return (
        _zone_of(tenant_dir(tenant, tenants_root=root), person)
        or load_tenant(tenant, tenants_root=root).identity.timezone
    )


def _zone_of(directory: Path, person: str) -> str:
    """The zone onboarding wrote beside ``person`` in authority.toml, or ""."""
    zone = person_entry(directory, person).get("zone", "")
    try:
        return zone if isinstance(zone, str) and zone and ZoneInfo(zone) else ""
    except (ZoneInfoNotFoundError, ValueError):
        return ""  # a hand-edited zone this host doesn't know: the ministry's


def kind_of(data: bytes) -> str:
    """The file's real type from its first bytes, as a suffix; "" for
    anything that is not a photo or a PDF, whatever it claims to be."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"%PDF-"):
        return ".pdf"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"heim", b"hevc"):
        return ".heic"
    return ""


def take_photo(
    desk: ReceiptDesk,
    token: str,
    data: bytes,
    *,
    tenant: str,
    tenants_root: str | Path | None,
    ledger_root: Path,
    now: datetime | None = None,
    name: str = "",
) -> tuple[int, dict]:
    """One upload from the receipt page: (status, body). The photo lands in
    the person's own folder, named with the day on their own clock, and
    intake files it; the link is spent only on a photo or a PDF, so a wrong
    file never costs the person their link. ``name`` is the name her phone
    gave the file: a statement's says whose account it is, at times
    (t-account-from-name)."""
    suffix = kind_of(data)
    if not suffix:
        return _take_file(desk, token, data, name)
    ticket, why = desk.take(token)
    if ticket is None:
        return (404 if why == "unknown" else 410), {"say": _GONE[why]}
    root = Path(tenants_root) if tenants_root else None
    cfg = load_tenant(tenant, tenants_root=root)
    folder = Path(cfg.expenses.drop_dir) / ticket.name / FUND
    folder.mkdir(parents=True, exist_ok=True)
    zone = _zone_of(tenant_dir(tenant, tenants_root=root), ticket.person) or cfg.identity.timezone
    claim = settled(ticket.claim, zone) if ticket.claim is not None else None
    day = local_today(zone, at=now or datetime.now(UTC))
    if ticket.pile:
        # A bank statement's photo or PDF, where the ministry's file says
        # so (card R2): proposed only when its lines add up.
        statement = _read_statement(desk, cfg, ticket, data, suffix, tenant, ledger_root, day, name)
        if statement is not None:
            return 200, statement
    # Tim reads the photo now, when the ministry's file says so (card R1),
    # each photo in a pile too (the owner's read of 2026-11-19). The photo is
    # in hand and nothing is filed yet: when what she said and what the
    # receipt prints disagree, it files with neither amount.
    reading = _read_now(cfg, ticket, claim, data, suffix, tenant, ledger_root, day)
    asking = reading is not None and claim is not None
    asking = asking and not reading.agrees(claim.amount, claim.currency)
    tag = claim.tag() if claim is not None and not asking else "unread"
    tag = "undated" if ticket.pile and reading is None else tag
    stem = f"{day}_receipt_{tag}"
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path, n = folder / f"{stem}-{n}{suffix}", n + 1
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)  # intake never sees half a photo

    from ..engine.runner import door_run as run  # the runner loads every agent; only on an upload

    # ledger_root is this tenant's own ledger (<base>/<tenant>); the runner
    # takes the base and adds the tenant itself.
    # The receipt's month is the month of the day in its name, the person's
    # own clock at the moment of the photo. Left to intake's own clock, a
    # photo taken in Kathmandu on the morning of the 1st filed under the
    # month a church in Ohio was still in.
    filed = run(
        tenant,
        "expenses",
        "intake",
        params={"month": day[:7]},
        ledger_dir=Path(ledger_root).parent,
        tenants_root=root,
    )
    if ticket.pile and reading is None:
        # A catch-up receipt Tim didn't read waits for its own day and
        # amount, read or said later; nothing about it is guessed here.
        return 200, {
            "ok": True,
            "say": "Got it."
            if filed.status == "ok"
            else "It's saved, and it goes in with the rest shortly.",
        }
    placed = ""
    # An institution whose own system places receipts (AGWM's Sage) gets
    # none of Tim's guesses ([institution].categorizes, 2026-10-13).
    guess = (
        from_words(ticket.said, list(cfg.expenses.category_accounts))
        if cfg.institution.categorizes
        else ""
    )
    if (guess or not cfg.institution.categorizes) and ticket.said and filed.status == "ok":
        # Their own words placed it ("the bus to Pokhara" is Travel), recorded
        # as said by them; the reviewer can still move it. An institution
        # that places receipts itself still gets the words, with no category.
        put = run(
            tenant,
            "expenses",
            "categorize",
            params={
                "sha": hashlib.sha256(data).hexdigest(),
                "category": guess,
                "by": ticket.person,
                "said": ticket.said,
            },
            ledger_dir=Path(ledger_root).parent,
            tenants_root=root,
        )
        placed = f" under {guess}" if guess and put.status in ("ok", "needs_approval") else ""
    if reading is not None and filed.status == "ok":
        read_back = _read_back(
            desk, cfg, ticket, claim, reading, data, tenant, root, ledger_root, day, bool(placed)
        )
        if read_back is not None:
            return 200, read_back
    if ticket.claim is None:
        return 200, {"ok": True, "say": _unread(cfg, data, filed.status, tenant, root, ledger_root)}
    said = f"Got it: {ticket.claim.words()}{placed}. " + (
        "It's in with this month's receipts."
        if filed.status == "ok"
        else "It's saved, and it goes in with this month's receipts shortly."
    )
    if ticket.claim.currency != "USD":
        said += " Its dollar amount is set with your monthly report."
    return 200, {"ok": True, "say": said}


def _read_now(
    cfg: Any,
    ticket: Ticket,
    claim: Claim | None,
    data: bytes,
    suffix: str,
    tenant: str,
    ledger_root: Path,
    day: str,
) -> Any:
    """What the photo reads, when the ministry's file switches the reader
    on (card R1), each photo of a pile too (2026-11-19), or None: an amount
    said in money the engine can't name, a photo too large to show, or no
    reading at all leaves the receipt exactly where it always went."""
    from . import receipt_read

    if not receipt_read.on(cfg.llm) or len(data) > MAX_SHOWN:
        return None
    if claim is not None and not claim.currency:
        return None
    return receipt_read.read(
        data,
        suffix,
        settings=cfg.llm,
        ledger_root=ledger_root,
        tenant=tenant,
        person=ticket.person,
        today=date.fromisoformat(day),
    )


def _read_statement(
    desk: ReceiptDesk,
    cfg: Any,
    ticket: Ticket,
    data: bytes,
    suffix: str,
    tenant: str,
    ledger_root: Path,
    day: str,
    name: str = "",
) -> dict | None:
    """A catch-up file read as a bank statement (card R2): its lines
    proposed for her tap when code proves they add up to the balances the
    statement prints; her bank's CSV asked for, and nothing kept, when they
    don't. None when the reader is off, the file is too large to show, the
    reader has no reading, or it reads as something else: the file goes on
    the way it always has."""
    from . import statement_read

    if not statement_read.on(cfg.llm) or len(data) > MAX_SHOWN:
        return None
    reply = statement_read.read(
        data,
        suffix,
        settings=cfg.llm,
        ledger_root=ledger_root,
        tenant=tenant,
        person=ticket.person,
        today=date.fromisoformat(day),
    )
    if reply is None:
        return None
    proven, why = statement_read.check(reply, date.fromisoformat(day))
    lines = statement_read.words()
    if proven is None:
        if why == statement_read.NOT_A_STATEMENT:
            return None
        return {"ok": True, "say": lines["csv"], "note": lines["csv"]}
    said = statement_read.proposal(proven)
    token = desk.propose(ticket, proven.rows, name)
    choices = [{"pick": "keep", "label": lines["keep"]}, {"pick": "no", "label": lines["no"]}]
    return {"ok": True, "say": said, "note": said, "ask": {"token": token, "choices": choices}}


def keep_statement(desk: ReceiptDesk, token: str, pick: str) -> tuple[int, dict]:
    """Her tap on a statement Tim read (card R2). "Keep them" holds its
    lines exactly as a CSV's are held, for her word on whose account it is;
    "No" keeps nothing."""
    from . import statement_read

    lines = statement_read.words()
    proposed = desk.accept(token)
    if proposed is None or pick not in ("keep", "no"):
        return 410, {"say": lines["gone"]}
    if pick == "no":
        return 200, {"ok": True, "say": lines["not_kept"]}
    ticket, rows, name = proposed
    desk.hold(ticket, STATEMENT, rows, file_name=name)
    return 200, {"ok": True, "say": _got_statement(rows)}


def _read_back(
    desk: ReceiptDesk,
    cfg: Any,
    ticket: Ticket,
    claim: Claim | None,
    reading: Any,
    data: bytes,
    tenant: str,
    root: Path | None,
    ledger_root: Path,
    day: str,
    placed: bool,
) -> dict | None:
    """The page's answer once Tim has read the photo (card R1): the reading
    recorded as a proposal, the way record_reading records one, and said
    back in one line. With no amount from her, "Right?" and her tap; with
    hers and the receipt's apart, which is right; with the two the same,
    it's in. None when the books were too busy to record it, so the page
    answers the way it always has."""
    from . import receipt_read

    lines = receipt_read.words()
    today = date.fromisoformat(day)
    what = reading.words(today)
    sha = hashlib.sha256(data).hexdigest()
    same = claim is not None and reading.agrees(claim.amount, claim.currency)
    # An institution whose own system places receipts gets none of Tim's
    # guesses ([institution].categorizes), the photo's included.
    category = "" if cfg.institution.categorizes else reading.category
    done = record_read(
        tenant,
        root,
        ledger_root,
        sha=sha,
        claim=None if same else from_tag(reading.tag()),
        day=reading.day,
        payee=reading.vendor,
        category=category,
        by=ticket.person,
    )
    if done == "error":
        return None
    no_day = reading.day is None  # a ticket's travel date is no day paid: Tim asks her
    if same:
        said = lines["same_no_day" if no_day else "same"].format(what=what)
        if reading.currency != "USD":
            said += " Its dollar amount is set with your monthly report."
        return {"ok": True, "say": said}
    if claim is None:
        choices = {
            "right": (reading.tag(), f"{lines['right']}: {what}"),
            "no": ("", ""),
        }
        labels = [("right", lines["right"]), ("no", lines["not_right"])]
        say = lines["reads_no_day" if no_day else "reads"].format(what=what)
    else:
        shown = receipt_read.money(reading.currency, reading.amount)
        theirs = receipt_read.money(claim.currency, claim.amount)
        choices = {
            "shown": (reading.tag(), f"{shown}, what the receipt shows"),
            "said": (claim.tag(), ticket.said),
        }
        labels = [("shown", shown), ("said", theirs)]
        say = lines["which"].format(shown=shown, said=theirs)
    token = desk.ask(ticket.person, sha, choices, "" if placed else category, no_day=no_day)
    ask = {"token": token, "choices": [{"pick": k, "label": v} for k, v in labels]}
    if claim is not None:
        ask["even"] = True  # which is right: neither answer is the easy one
    out: dict = {"ok": True, "say": say, "ask": ask}
    if ticket.pile:  # listed with the rest of the pile, for her one yes
        out["read"] = what + (lines["no_day_mark"] if no_day else "")
        if no_day:
            out["no_day"] = True
    return out


def answer_read(
    desk: ReceiptDesk,
    token: str,
    pick: str,
    *,
    tenant: str,
    tenants_root: str | Path | None,
    ledger_root: Path,
) -> tuple[int, dict]:
    """Her tap on the page's question about a receipt it read (card R1).
    The amount she picks becomes her word, the same yes agree_to_readings
    gives; "No" sets nothing, and Tim asks how much."""
    from . import receipt_read

    lines = receipt_read.words()
    asked, why = desk.answer(token)
    if asked is None or pick not in asked.choices:
        return (404 if why == "unknown" else 410), {"say": lines["gone"]}
    tag, said = asked.choices[pick]
    if not tag:
        return 200, {"ok": True, "say": lines["not_counted"]}
    agree_read(
        tenant,
        Path(tenants_root) if tenants_root else None,
        ledger_root,
        sha=asked.sha,
        tag=tag,
        category=asked.category if pick != "said" else "",
        said=said,
        by=asked.person,
    )
    return 200, {"ok": True, "say": lines["counted_no_day" if asked.no_day else "counted"]}


def _text_of(data: bytes) -> str | None:
    """A file's text, when it's text at all."""
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _take_file(desk: ReceiptDesk, token: str, data: bytes, name: str = "") -> tuple[int, dict]:
    """A statement or a school list through a catch-up link (t-statements):
    read by code, held in memory for the person's word, never written; the
    link is spent on nothing, so a wrong file costs nothing."""
    text = _text_of(data)
    if text is None:
        return 415, {"say": "That isn't a photo. Tap the button and take a picture of it."}
    from ..agents.expenses import processors

    more: dict = {}
    try:
        if processors.is_export(text):  # PayPal, a giving app (c32)
            export = processors.parse(text)
            kind, rows = EXPORT, export.payments
            more = {"service": export.service, "left_out": export.left_out}
        elif statements.is_school_list(text):
            kind, rows = SCHOOL_LIST, tuple(statements.parse_school(text))
        else:
            kind, rows = STATEMENT, tuple(statements.parse(text))
    except (statements.NotAStatement, processors.NotAnExport) as exc:
        rows, why = (), exc.say
    else:
        why = ""
    if not rows:
        return 415, {
            "say": why
            or "That file isn't a bank statement Tim can read. Send the CSV file your "
            "bank's website exports, or a photo of a receipt."
        }
    if not desk.is_pile(token):
        # Read the link's state; spending it here would cost the person the
        # photo they meant to send (the weekly attacker, 2026-10-16).
        why = desk.gone(token)
        if why:
            return (404 if why == "unknown" else 410), {"say": _GONE[why]}
        return 415, {"say": "A statement goes in through a catch-up link. Ask Tim for one."}
    ticket, _why = desk.take(token)
    held = desk.hold(ticket, kind, rows, file_name=name, **more)
    if kind == EXPORT:
        said = _export_words(held)
        return 200, {"ok": True, "say": said, "note": said}
    if kind == SCHOOL_LIST:
        said = f"Got your school list: {len(rows)} payments. Tell Tim to add it."
        return 200, {"ok": True, "say": said, "note": said}
    said = _got_statement(rows)
    return 200, {"ok": True, "say": said, "note": said}


def _got_statement(rows: tuple) -> str:
    """What the page says of a statement it holds for her word, from a CSV
    or from her tap on one Tim read (card R2)."""
    first, last = date.fromisoformat(rows[0].day), date.fromisoformat(rows[-1].day)
    return (
        f"Got your statement: {len(rows)} lines, {first:%b} {first.day} to {last:%b} "
        f"{last.day}. Tell Tim whose account it is: the ministry's, or your own."
    )


def _unread(
    cfg: Any, data: bytes, filed: str, tenant: str, root: Path | None, ledger_root: Path
) -> str:
    """A photo that came with no amount: what it reads, as a proposal for the
    person's yes, or the question."""
    from ..engine.runner import door_run as run

    reader = reader_for(cfg.expenses.reader)
    reading = reader.read(data) if reader is not None and filed == "ok" else None
    if reading is not None:
        try:
            claim = Claim(Decimal(reading.amount).quantize(Decimal("0.01")), reading.currency, "")
        except ArithmeticError:
            claim = None
        if claim is not None and reading.currency in _CODES:
            claim = Claim(
                claim.amount, claim.currency, "$" if claim.currency == "USD" else claim.currency
            )
            run(
                tenant,
                "expenses",
                "amount",
                params={
                    "sha": hashlib.sha256(data).hexdigest(),
                    "tag": claim.tag(),
                    "read": "photo",
                },
                ledger_dir=Path(ledger_root).parent,
                tenants_root=root,
            )
            return (
                f"Got it. The photo reads {claim.words()}. If that's right, tell Tim, and "
                "it counts in this month's receipts."
            )
    return "Got it. How much was it? Tell Tim, and it counts in this month's receipts."


def upload(
    desk: ReceiptDesk,
    headers: Any,
    read: Callable[[int], bytes],
    *,
    origin: str,
    tenant: str,
    tenants_root: str | Path | None,
    ledger_root: Path,
) -> tuple[int, dict, bool]:
    """One POST to the receipt page, from headers and body to (status, body,
    close the connection): the page's own origin, a size it will read, then
    the photo. Too large is refused before the body is read."""
    given = headers.get("Origin")
    if given and given != origin:
        return 403, {"say": "That didn't come from Tim's page."}, False
    length = int(headers.get("Content-Length") or 0)
    asked = str(headers.get("X-Receipt-Reading") or "")
    if asked:  # her tap on what the page read (card R1): a few bytes, never a photo
        if length > MAX_PICK:
            return 413, {"say": "That didn't come from Tim's page."}, True
        raw = read(length) if length > 0 else b""
        pick = parse_qs(raw.decode("utf-8", "replace")).get("pick", [""])[0]
        if desk.proposed(asked):  # a statement Tim read (card R2)
            status, body = keep_statement(desk, asked, pick)
            return status, body, False
        status, body = answer_read(
            desk, asked, pick, tenant=tenant, tenants_root=tenants_root, ledger_root=ledger_root
        )
        return status, body, False
    if length > MAX_PHOTO:
        return 413, {"say": "That photo is too large. Try again a little closer."}, True
    data = read(length) if length > 0 else b""
    token = str(headers.get("X-Receipt-Ticket") or "")
    status, body = take_photo(
        desk,
        token,
        data,
        tenant=tenant,
        tenants_root=tenants_root,
        ledger_root=ledger_root,
        name=file_name(headers),
    )
    return status, body, False


def file_name(headers: Any) -> str:
    """The name the page says her phone gave the file (``X-Receipt-Name``,
    URL-encoded), its last part only and never past ``MOST`` characters;
    "" when it gave none."""
    from .statement_names import MOST

    raw = unquote(str(headers.get("X-Receipt-Name") or "")[: MOST * 3])
    return " ".join(re.split(r"[\\/]", raw)[-1].split())[:MOST]


_GONE = {
    "used": "This link was already used. Ask Tim for a new one.",
    "expired": "This link has expired. Ask Tim for a new one.",
    "unknown": "This link no longer works (Tim may have restarted). Ask Tim for a new one.",
}

# Tim's look, copied from the doorkeeper's pages (doorkeeper/pages.py): the
# box's two programs share no code across the sign-in boundary, so the style
# is a copy, kept small and loaded from nowhere else (the 10/14 board item).
_STYLE = """
:root{--bg:#FAF6EE;--surface:#fff;--text:#16233F;--text-2:#55617A;--rule:#E6E0D2;
--accent:#D3A24B;--btn:#16233F;--btn-text:#FAF6EE;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0B1526;--surface:#121F38;--text:#F3EFE4;
--text-2:#A9B2C9;--rule:#1F2D4A;--accent:#E3B563;--btn:#E3B563;--btn-text:#0B1526}}
body{font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
background:var(--bg);color:var(--text)}
.wrap{max-width:26rem;margin:0 auto;padding:1.1rem .75rem 2rem}
.top{display:flex;align-items:center;gap:.6rem;margin:0 .25rem .8rem}
.mark{width:2.1rem;height:2.1rem;flex:none}
.top b{display:block;font-size:1.05rem;line-height:1.2}
.who{display:block;font-size:.85rem;color:var(--text-2);line-height:1.3}
main{background:var(--surface);border:1px solid var(--rule);border-radius:12px;
padding:1.2rem 1.1rem}
h1{font-size:1.35rem;line-height:1.25;margin:0 0 .9rem}
label,button{display:block;box-sizing:border-box;width:100%;text-align:center;font:inherit;
font-weight:600;padding:.85rem;border:0;border-radius:8px;background:var(--btn);
color:var(--btn-text);cursor:pointer}
button{margin-top:.7rem}
label.again{background:none;color:var(--text);border:1px solid var(--rule);font-weight:400}
input{display:none}
label.busy{opacity:.5;pointer-events:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
progress{width:100%;accent-color:var(--accent)}
ul{margin:.8rem 0 0;padding-left:1.2rem;overflow-wrap:anywhere}
.note{color:var(--text-2);font-size:.95rem}
#status{white-space:pre-line}
[hidden]{display:none!important}
"""

_MARK = (
    '<svg class="mark" viewBox="0 0 1000 1000" aria-hidden="true">'
    '<rect width="1000" height="1000" rx="220" fill="#16233F"/>'
    '<circle cx="500" cy="485" r="315" fill="#FFF"/>'
    '<path d="M297.5,726.3 Q390,768 412,858 Q492,828 450.7,796.1 Z" fill="#FFF"/>'
    '<path d="M500,280 L500,635 C500,685 540,715 592,700 M392,430 L608,430" fill="none" '
    'stroke="#16233F" stroke-width="50" stroke-linecap="round"/>'
    '<circle cx="405" cy="895" r="50" fill="#D3A24B"/></svg>'
)


def shell(
    title: str,
    body: str,
    nonce: str,
    *,
    ministry: str = "",
    more: str = "",
    head: str = "",
    script: str = "",
) -> str:
    """A page in Tim's look: his mark, whose Tim it is, ``body`` on one
    card, the page's own extra style (``more``) and ``head`` lines, and its
    one ``script``, each under this response's nonce. Every person-facing
    page the engine serves wears it (the month, the setup questions, the
    talk page); the caller escapes ``body``."""
    who = f'<span class="who">for {escape(ministry)}</span>' if ministry else ""
    js = f'<script nonce="{escape(nonce)}">{script}</script>' if script else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>{head}"
        f'<style nonce="{escape(nonce)}">{_STYLE}{more}</style>'
        f'</head><body><div class="wrap"><header class="top">{_MARK}<span><b>Tim</b>{who}'
        f"</span></header><main>{body}</main></div>{js}</body></html>"
    )


def app_manifest(start: str, name: str, short_name: str) -> dict:
    """A home-screen entry that opens straight to ``start``, wearing Tim's
    mark (the icons ``icon`` serves)."""
    return {
        "name": name,
        "short_name": short_name,
        "start_url": start,
        "scope": start,
        "display": "standalone",
        "background_color": "#FAF6EE",
        "theme_color": "#16233F",
        "icons": [
            {"src": f"/receipt/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"}
            for n in (192, 512)
        ],
    }


_ASK_STYLE = (
    "button.again{background:none;color:var(--text);border:1px solid var(--rule);font-weight:400}"
)
# The page's question about a photo Tim read (card R1): her answers as
# buttons, each one post back to the page. Only a page whose ministry has the
# reader on carries it; every other page is the same, byte for byte.
_ASK_SCRIPT = (
    """
const box=document.getElementById('ask');
const answer=(token,choice)=>{box.hidden=true;const x=new XMLHttpRequest();
x.open('POST','"""
    + PAGE_PATH
    + """');x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');
x.setRequestHeader('X-Receipt-Reading',token);
x.onload=()=>{let d={};try{d=JSON.parse(x.responseText);}catch(y){}say(d.say||'Saved.');
if(home){label.hidden=false;}};
x.onerror=()=>{box.hidden=false;say("That didn't go through. Tap it again.");};
x.send('pick='+encodeURIComponent(choice));};
const asked=q=>{if(!q){return;}label.hidden=true;
box.replaceChildren(...q.choices.map((c,n)=>{const b=document.createElement('button');
b.type='button';b.textContent=c.label;if(n&&!q.even){b.className='again';}
b.onclick=()=>answer(q.token,c.pick);return b;}));box.hidden=false;};"""
)


# A catch-up page's questions about statements Tim read (card R2): each one
# asked after the pile is sent, one at a time, her tap a post back to the
# page. Only a page whose ministry has the statement reader on carries it.
_PILE_ASK_SCRIPT = (
    """
const box=document.getElementById('ask'),asks=[];
const answer=(token,choice)=>{box.hidden=true;const x=new XMLHttpRequest();
x.open('POST','"""
    + PAGE_PATH
    + """');x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');
x.setRequestHeader('X-Receipt-Reading',token);
x.onload=()=>{let d={};try{d=JSON.parse(x.responseText);}catch(y){}const q=asks.shift();
say((d.say||'Saved.')+(q?'\\n\\n'+q.say:''));asked(q);};
x.onerror=()=>{box.hidden=false;say("That didn't go through. Tap it again.");};
x.send('pick='+encodeURIComponent(choice));};
const asked=q=>{if(!q){label.hidden=false;return;}label.hidden=true;
box.replaceChildren(...q.choices.map((c,n)=>{const b=document.createElement('button');
b.type='button';b.textContent=c.label;if(n){b.className='again';}
b.onclick=()=>answer(q.token,c.pick);return b;}));box.hidden=false;};"""
)


# A pile Tim read (the owner's read of 2026-11-19): every receipt he read,
# listed once the pile is sent, each one she can set aside, and one yes that
# counts the rest, posted one by one; then any statement's question, one at a
# time, as on a pile with the statement reader alone. Only a pile page whose
# ministry has the reader on carries it.
_PILE_READ_SCRIPT = (
    """
const box=document.getElementById('ask'),asks=[],reads=[];let unread=0;const W=__WORDS__;
const n_=(k,n)=>(n===1?W[k+'_one']:W[k].replace('{n}',n));
const post=(token,choice,done)=>{const x=new XMLHttpRequest();x.open('POST','"""
    + PAGE_PATH
    + """');x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');
x.setRequestHeader('X-Receipt-Reading',token);
x.onload=()=>{let d={};try{d=JSON.parse(x.responseText);}catch(y){}done(x.status,d);};
x.onerror=()=>done(0,{});x.send('pick='+encodeURIComponent(choice));};
const answer=(token,choice)=>{box.hidden=true;post(token,choice,(s,d)=>{
if(!s){box.hidden=false;say("That didn't go through. Tap it again.");return;}
const q=asks.shift();say((d.say||'Saved.')+(q?'\\n\\n'+q.say:''));asked(q);});};
const asked=q=>{if(!q){label.hidden=false;return;}label.hidden=true;
box.replaceChildren(...q.choices.map((c,n)=>{const b=document.createElement('button');
b.type='button';b.textContent=c.label;if(n){b.className='again';}
b.onclick=()=>answer(q.token,c.pick);return b;}));box.hidden=false;};
const summary=()=>{const lost=unread?n_('pile_unread',unread):'';unread=0;
if(!reads.length){if(lost){say(document.getElementById('status').textContent+'\\n\\n'+lost);}
asked(asks.shift());return;}
label.hidden=true;const head=document.createElement('p');
head.textContent=n_('pile_read',reads.length)+(lost?' '+lost:'');
const ul=document.createElement('ul');ul.className='reads';
reads.forEach(r=>{const li=document.createElement('li'),t=document.createElement('span');
t.textContent=r.what;const b=document.createElement('button');b.type='button';b.className='again';
b.textContent=W.pile_set_aside;b.onclick=()=>{r.no=!r.no;li.classList.toggle('off',r.no);
b.textContent=r.no?W.pile_put_back:W.pile_set_aside;};li.append(t,b);ul.append(li);});
const yes=document.createElement('button');yes.type='button';
yes.textContent=reads.length===1?W.pile_right_one:W.pile_right;
yes.onclick=()=>{box.hidden=true;say(W.pile_counting);let i=0,counted=0,aside=0,missed=0,noDay=0;
const step=()=>{if(i>=reads.length){reads.length=0;
say([counted?n_('pile_counted',counted):'',noDay?n_('pile_no_day',noDay):'',
aside?n_('pile_aside',aside):'',
missed?n_('pile_missed',missed):''].filter(Boolean).join(' '));asked(asks.shift());return;}
const r=reads[i++];post(r.token,r.no?'no':'right',(s,d)=>{
if(s!==200){missed++;}else if(r.no){aside++;}else{counted++;if(r.noDay){noDay++;}}step();});};
step();};
box.replaceChildren(head,ul,yes);box.hidden=false;};"""
)
_PILE_READ_STYLE = (
    "ul.reads{list-style:none;padding:0;margin:.5em 0 1em}"
    "ul.reads li{display:flex;gap:.75em;align-items:center;justify-content:space-between;"
    "padding:.5em 0;border-bottom:1px solid var(--rule)}"
    "ul.reads li button{width:auto;margin:0;padding:.4em .8em;flex:none}"
    "ul.reads li.off span{text-decoration:line-through;opacity:.6}"
)


def _pile_read_script() -> str:
    """:data:`_PILE_READ_SCRIPT` with the page's own words in it."""
    from . import receipt_read

    lines = {k: v for k, v in receipt_read.words().items() if k.startswith("pile_")}
    return _PILE_READ_SCRIPT.replace("__WORDS__", json.dumps(lines).replace("</", "<\\/"))


def page(
    nonce: str,
    *,
    many: bool = False,
    ministry: str = "",
    home: str = "",
    reads: bool = False,
    statements: bool = False,
) -> str:
    """The receipt page: one button that opens the camera; or, for a
    catch-up link (``many``), one button that picks every receipt at once
    from the camera roll or Files, lists each file by name, and sends them
    one by one on a second tap (the 10/14 phone run: the picker once sent a
    real photo by mistake). A failed send says the box's own reason. The
    link's token rides in the address's #fragment, which never reaches a
    server log.

    With ``home`` (a standing link's key, chunk 10), the page is the one on
    the person's home screen: it carries its own home-screen entry and icon,
    shows how to add it until it opens from there, and asks the box for a
    fresh one-use ticket before each photo.

    With ``reads`` (the ministry's reader is on, card R1), a photo Tim read
    comes back with its question, and her answer is a tap here. With
    ``statements`` (its statement reader is on, card R2), a catch-up page
    asks about each statement Tim read once the pile is sent."""
    title = "Send your receipts" if many else "Add your receipt"
    pile_reads = reads and many  # each photo of the pile read (2026-11-19)
    reads = reads and not many
    statements = statements and many
    ask_style = _ASK_STYLE if reads or statements or pile_reads else ""
    ask_style += _PILE_READ_STYLE if pile_reads else ""
    ask_box = "\n<div id=ask hidden></div>" if reads or statements or pile_reads else ""
    ask_call = "asked(d.ask);" if reads else ""
    ask_script = _ASK_SCRIPT if reads else _PILE_ASK_SCRIPT if statements else ""
    pile_push = "if(d.ask){asks.push({...d.ask,say:d.say});}" if statements else ""
    pile_end = "asked(asks.shift());" if statements else ""
    if pile_reads:
        ask_script = _pile_read_script()
        pile_push = (
            "if(d.read&&d.ask){reads.push({token:d.ask.token,what:d.read,noDay:!!d.no_day});}"
            "else if(d.ask){asks.push({...d.ask,say:d.say});}else if(!d.note){unread++;}"
        )
        pile_end = "summary();"
    # A catch-up file's name says whose account a statement is, at times
    # (t-account-from-name); a camera photo's name says nothing.
    name_header = (
        "\nx.setRequestHeader('X-Receipt-Name',encodeURIComponent(f.name||''));" if many else ""
    )
    who = f'<span class="who">for {escape(ministry)}</span>' if ministry else ""
    if many:
        pick = (
            "<label>Choose your receipts<input id=photo type=file "
            'accept="image/*,application/pdf,.csv,text/csv" multiple></label>'
            '<ul id="files" hidden></ul><button id="send" type=button hidden></button>'
            "<p class=note>Statements or receipts in iCloud Drive? Tap the button, choose "
            "Choose File, then search for the folder by its name at the top. On an iPhone, "
            "switch the picker's view to List, and your CSV files are easy to spot.</p>"
        )
    else:
        pick = (
            '<label>Take the photo<input id=photo type=file accept="image/*,application/pdf"\n'
            'capture="environment"></label>'
        )
    installable = add_it = ""
    if home:
        key = escape(home)
        installable = (
            f'\n<link rel="manifest" href="{HOME_PATH}{key}/app.webmanifest">'
            '\n<link rel="apple-touch-icon" href="/receipt/icon-180.png">'
            '\n<meta name="apple-mobile-web-app-capable" content="yes">'
            '\n<meta name="apple-mobile-web-app-title" content="Receipts">'
        )
        add_it = (
            "\n<div id=addit><p class=note><b>Put this page on your home screen.</b> "
            "On an iPhone: tap Share (the square with the arrow), then Add to Home Screen. "
            "On Android: tap the menu (three dots), then Add to Home screen. Then open it "
            "from there.</p></div>"
        )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>{installable}
<style nonce="{nonce}">{_STYLE}{ask_style}</style></head><body>
<div class="wrap"><header class="top">{_MARK}<span><b>Tim</b>{who}</span></header><main>
<h1>{title}</h1>
{pick}{add_it}
<progress id=bar max=100 value=0 hidden></progress>
<p id=status role=status></p>{ask_box}
</main></div>
<script nonce="{nonce}">
const many={"true" if many else "false"},home={"true" if home else "false"};
let ticket=home?'':location.hash.slice(1);
const fresh=async()=>{{
const standalone=matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const form={{'Content-Type':'application/x-www-form-urlencoded'}};
const r=await fetch(location.pathname,{{method:'POST',headers:form,
body:'standalone='+(standalone?1:0)}});let d={{}};try{{d=await r.json();}}catch(y){{}}
if(r.ok){{ticket=d.ticket;}}else{{ticket='';label.hidden=true;
say(d.say||'This page no longer works. Ask Tim for a new one.');}}
const tip=document.getElementById('addit');if(tip){{tip.hidden=standalone;}}}};
const say=t=>{{document.getElementById('status').textContent=t;}};
const bar=document.getElementById('bar'),label=document.querySelector('label');
const list=document.getElementById('files'),go=document.getElementById('send');
let chosen=[];{ask_script}
const send=(f,done)=>{{
const x=new XMLHttpRequest();x.open('POST','{PAGE_PATH}');x.withCredentials=false;
x.setRequestHeader('Content-Type',f.type||'application/octet-stream');
x.setRequestHeader('X-Receipt-Ticket',ticket);{name_header}
x.upload.onprogress=p=>{{if(p.lengthComputable){{bar.value=Math.round(100*p.loaded/p.total);}}}};
x.onload=()=>{{let d={{}};try{{d=JSON.parse(x.responseText);}}catch(y){{}}done(x.status,d);}};
x.onerror=()=>done(0,{{}});x.send(f);}};
const run=files=>{{
label.classList.add('busy');if(go){{go.hidden=true;}}bar.hidden=false;
let sent=0,missed=0,i=0;const notes=[],reasons=[];
const next=()=>{{
if(i>=files.length){{bar.hidden=true;label.classList.remove('busy');
if(!many){{return;}}
say((missed?`Sent ${{sent}} of ${{files.length}}. ${{missed}} didn't go through. `
+(reasons.length?reasons.join(' '):'Tap the button and choose them again.')
:`All ${{sent}} sent. You can choose more, or go back to Tim.`)
+(notes.length?'\\n\\n'+notes.join('\\n\\n'):''));{pile_end}return;}}
const f=files[i++];bar.value=0;
say(many?`Sending ${{i}} of ${{files.length}}...`:'Sending your photo...');
send(f,(status,d)=>{{
if(status===200){{sent++;if(d.note){{notes.push(d.note);}}{pile_push}
if(!many){{say(d.say||'Saved.');{ask_call}
if(home){{label.firstChild.textContent='Take another';fresh();}}else{{label.hidden=true;}}}}}}
else{{missed++;
const why=status?(d.say||'That did not go through. Ask Tim for a new link.')
:"Your link still works: tap the button to try again.";
if(many){{if(!reasons.includes(why)){{reasons.push(why);}}}}
else{{say(status?why:"That didn't go through. "+why);}}}}
next();}});}};
next();}};
if(home){{fresh();}}
document.getElementById('photo').onchange=e=>{{
const files=Array.from(e.target.files);e.target.value='';if(!files.length)return;
if(!many){{run(files);return;}}
chosen=files;list.replaceChildren(...files.map(f=>{{const li=document.createElement('li');
li.textContent=f.name;return li;}}));list.hidden=false;
go.textContent=`Send these ${{files.length}}`;go.hidden=false;
label.firstChild.textContent='Choose again';label.classList.add('again');say('');}};
if(go){{go.onclick=()=>{{list.hidden=true;run(chosen);}};}}
</script></body></html>"""


__all__ = [
    "FUND",
    "HOME_PATH",
    "LINKS_FILE",
    "HomeLinks",
    "add_home_link",
    "drop_home_link",
    "home_person",
    "home_route",
    "app_manifest",
    "icon",
    "manifest",
    "shell",
    "MAX_PHOTO",
    "PAGE_PATH",
    "Asked",
    "Claim",
    "ReceiptDesk",
    "agree_read",
    "answer_read",
    "record_read",
    "Receipts",
    "Ticket",
    "claim_in",
    "filer_name",
    "person_entry",
    "kind_of",
    "page",
    "take_photo",
    "upload",
]
