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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agents.expenses import statements
from ..agents.expenses.categories import UNCATEGORIZED, canonical, from_words
from ..authority import Request, Role, evaluate
from ..engine.clock import local_today
from ..engine.config import load_tenant, tenant_dir
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


@dataclass(frozen=True)
class Held:
    """A statement or a school list sent through a catch-up link, parsed and
    held in memory until the person says whose account it is (t-statements).
    Nothing of it is written anywhere: a line reaches the books only when
    it's the ministry's, or the person picks it; a restart forgets it."""

    id: str
    person: str
    name: str
    kind: str  # STATEMENT or SCHOOL_LIST
    rows: tuple
    by: str
    until: float


STATEMENT, SCHOOL_LIST = "bank statement", "school list"


@dataclass(frozen=True)
class Ticket:
    person: str  # their id in authority.toml
    name: str  # their name in the expense drop tree
    claim: Claim | None  # None: no amount in their words; read it or ask after
    said: str = ""  # their own words, for the receipt's category
    pile: bool = False  # a catch-up link: many receipts, for a day (t-catch-up)
    by: str = ""  # who asked for it: the person, or a reviewer for them


class ReceiptDesk:
    """The box's receipt links: a token, good once, for fifteen minutes; or
    a catch-up token, good for a day and for every send in it. Held in
    memory; a restart only means "ask Tim again"."""

    def __init__(self, *, ttl: float = TTL_SECONDS, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, tuple[Ticket, float]] = {}
        self._used: set[str] = set()
        self._held: dict[str, Held] = {}

    def issue(self, person: str, name: str, claim: Claim | None, said: str = "") -> str:
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._open[token] = (Ticket(person, name, claim, said), self.clock() + self.ttl)
        return token

    def issue_pile(self, person: str, name: str, *, by: str) -> str:
        token = secrets.token_urlsafe(18)
        ticket = Ticket(person, name, None, pile=True, by=by)
        with self._lock:
            self._open[token] = (ticket, self.clock() + PILE_TTL_SECONDS)
        return token

    def is_pile(self, token: str) -> bool:
        """Whether the token is a live catch-up link, without spending it."""
        with self._lock:
            held = self._open.get(token)
            return bool(held and held[0].pile and self.clock() <= held[1])

    def hold(self, ticket: Ticket, kind: str, rows: tuple) -> Held:
        """A parsed file, held for a day for the person's word."""
        held = Held(
            "st-" + secrets.token_urlsafe(6),
            ticket.person,
            ticket.name,
            kind,
            rows,
            ticket.by,
            self.clock() + PILE_TTL_SECONDS,
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

    def take(self, token: str) -> tuple[Ticket | None, str]:
        """The ticket, once (a catch-up ticket, until it expires):
        (ticket, "") or (None, "used" | "expired" | "unknown")."""
        with self._lock:
            if token in self._used:
                return None, "used"
            held = self._open.get(token)
            if held is None:
                return None, "unknown"
            ticket, expires = held
            if ticket.pile and self.clock() <= expires:
                return ticket, ""
            self._open.pop(token)
            self._used.add(token)
            return (ticket, "") if self.clock() <= expires else (None, "expired")


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
        self.name = _filer_name(tenant, self.tenants_root, person)

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
        from ..agents.expenses.jobs import LANDED_EVENT
        from ..engine.runner import run

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
        category = canonical(str(args.get("category") or ""), listed)
        if category is None:
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
                "category": category,
                "by": self.person,
                "said": said,
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
        return {
            "status": "placed",
            "say": f"Done: the {day} receipt is under {category or UNCATEGORIZED}. If the "
            "month is waiting for a yes, its card shows the change. Tell them so.",
        }

    def add(self, said: str) -> dict:
        claim = claim_in(said)
        if claim is not None and not claim.currency:
            root = self.tenants_root
            zone = (
                _zone_of(tenant_dir(self.tenant, tenants_root=root), self.person)
                or load_tenant(self.tenant, tenants_root=root).identity.timezone
            )
            if not settled(claim, zone).currency:
                return {
                    "status": "needs_currency",
                    "say": f"Ask them which currency {claim.words()} was in (for example Nepali "
                    "or Indian rupees), then call add_receipt again with their words.",
                }
        link = f"{self.base_url}#{self.desk.issue(self.person, self.name, claim, said)}"
        goes = (
            f"as {claim.words()}"
            if claim is not None
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
        names = [p.name for p in cfg.expenses.persons]
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
                    "say": f"Nobody named {asked} files receipts here. Ask who they meant.",
                },
            )
        if not self._may_place(t.viewer, unit_of(name)):
            return (
                "",
                "",
                {
                    "status": "refused",
                    "say": f"{name}'s receipts aren't in a month they review.",
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
        from ..agents.expenses.jobs import AMOUNT_EVENT, LANDED_EVENT, READ_EVENT

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
        from ..engine.runner import run

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
        sha = str(receipt.get("sha256", ""))
        root, ledger = self.tenants_root, Path(self.ledger_root).parent
        currency = str(args.get("currency") or "").strip().upper()
        try:
            amount = Decimal(str(args.get("amount"))).quantize(Decimal("0.01"))
        except ArithmeticError:
            amount = None
        params = {"sha": sha, "read": "photo"}
        for key in ("payee", "category"):
            said = str(args.get(key) or "").strip()[:120]
            if said:
                params[key] = said
        if amount is not None and amount > 0 and currency in _CODES:
            claim = Claim(amount, currency, "$" if currency == "USD" else currency)
            run(
                self.tenant,
                "expenses",
                "amount",
                params={**params, "tag": claim.tag()},
                ledger_dir=ledger,
                tenants_root=root,
            )
        done = run(
            self.tenant,
            "expenses",
            "date",
            params={"sha": sha, "day": day.isoformat(), "read": "photo", "by": self.person},
            ledger_dir=ledger,
            tenants_root=root,
        )
        if done.status == "error":
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
        from ..agents.expenses.jobs import LANDED_EVENT

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
        from ..agents.expenses.jobs import AMOUNT_EVENT, CATEGORY_EVENT, LANDED_EVENT, READ_EVENT
        from ..engine.runner import run

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
        ledger, root = Path(self.ledger_root).parent, self.tenants_root
        for sha, landed in latest.get(LANDED_EVENT, {}).items():
            reading = latest.get(READ_EVENT, {}).get(sha)
            if landed.get("person") != name or dated.get(sha, "")[:7] != month or not reading:
                continue
            if sha not in latest.get(AMOUNT_EVENT, {}):
                run(
                    self.tenant,
                    "expenses",
                    "amount",
                    params={"sha": sha, "tag": reading["tag"], "said": said, "by": person},
                    ledger_dir=ledger,
                    tenants_root=root,
                )
                agreed += 1
            if reading.get("category") and sha not in latest.get(CATEGORY_EVENT, {}):
                run(
                    self.tenant,
                    "expenses",
                    "categorize",
                    params={
                        "sha": sha,
                        "category": reading["category"],
                        "by": person,
                        "said": said,
                    },
                    ledger_dir=ledger,
                    tenants_root=root,
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
        from ..engine.runner import run

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
        from ..engine.runner import run

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
        from ..agents.expenses.jobs import DRIVE_EVENT, drive_id
        from ..engine.runner import run

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
        from ..agents.expenses.jobs import LANDED_EVENT, READ_EVENT, amounts_said
        from ..engine.runner import run

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
        if not asked:
            return {
                "statements": [_held_words(h) for h in held.values()],
                "say": "Each statement's id is for your calls; never say it to the person. "
                "Ask them whose account each bank statement is (the ministry's, or their own), "
                "then call bank_statement with its statement and account. A school list goes "
                "in with keep_school_list."
                if held
                else "Nothing they sent is waiting. Statements go in through their catch-up "
                "link (catch_up_link): the CSV file their bank's website exports.",
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
            f"{_day_words(h.rows[0].day)} to {_day_words(h.rows[-1].day)}. Each shows in its "
            "month (month_report's bank lines); a line that needs its receipt, or what it was "
            "for, says so. Money sent abroad, gifts in, and paying them back are never "
            "spending.",
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
        return {
            "status": "pick",
            "rows": rows,
            "say": "These are their own account's lines, for them alone. Walk them through the "
            "ones Tim marked (looks: what each looks like) and any others they name, in plain "
            "words: a row number is for your calls, never say it to the person. Then pass the "
            "rows they pick to keep_own_lines with their words. Nothing is kept until they "
            "pick, and the rest is never kept.",
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
            picks = sorted({int(r) for r in args.get("rows") or []})
        except (TypeError, ValueError):
            picks = []
        if not picks or picks[0] < 1 or picks[-1] > len(h.rows):
            return {"status": "which", "say": "Pass the rows they picked, from bank_statement."}
        picked = [h.rows[n - 1] for n in picks]
        ministry = [
            statements.Line.of(e)
            for e in self._kept_lines(t)
            if e.get("person") == h.name and e.get("account") == statements.MINISTRY
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
            f"The other {rest} were never kept and are gone from Tim.{paid} Tell them so.",
        }

    def keep_school(self, t: Any, args: dict) -> dict:
        """The person's school list: each payment a line with no receipt."""
        from ..engine.runner import run

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
            "day's rate.",
        }

    def explain_line(self, t: Any, args: dict) -> dict:
        """What one statement line was for, in the person's words."""
        from ..engine.runner import run

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
        category = from_words(said, listed)
        done = run(
            self.tenant,
            "expenses",
            "line_purpose",
            params={
                "sha": str(line["sha256"]),
                "said": said,
                "category": category,
                "by": self.person,
            },
            ledger_dir=Path(self.ledger_root).parent,
            tenants_root=self.tenants_root,
        )
        if done.status == "error":
            return _BUSY
        under = f" It's under {category}." if category else ""
        return {
            "status": "set",
            "say": f"Done: the {_day_words(str(line['day']))} line is in, as they said it.{under}",
        }

    def _kept_lines(self, t: Any) -> list[dict]:
        return [
            json.loads(r["payload_json"] or "{}")
            for r in t._rows(
                "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ? ORDER BY id",
                (t.tenant, statements.LINE_EVENT),
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
        from ..engine.runner import run

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


_BUSY = {"status": "busy", "say": "The books are busy for a moment. Try again in a minute."}


def _day_words(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}, {d.year}"


def _held_words(h: Held) -> dict:
    return {
        "statement": h.id,
        "kind": h.kind,
        "lines": len(h.rows),
        "from": h.rows[0].day,
        "to": h.rows[-1].day,
    }


def _day_of(said: Any, today: date) -> date | None:
    """A YYYY-MM-DD day that has come, or None."""
    try:
        day = datetime.strptime(str(said or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return day if day <= today else None


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


def _filer_name(tenant: str, tenants_root: Path | None, person: str) -> str:
    cfg = load_tenant(tenant, tenants_root=tenants_root)
    if not cfg.expenses.drop_dir:
        return ""
    return next((p.name for p in cfg.expenses.persons if unit_of(p.name) == person), "")


def _zone_of(directory: Path, person: str) -> str:
    """The zone onboarding wrote beside ``person`` in authority.toml, or ""."""
    try:
        people = tomllib.loads((directory / "authority.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    entry = people.get("people", {}).get(person, {})
    zone = entry.get("zone", "") if isinstance(entry, dict) else ""
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
) -> tuple[int, dict]:
    """One upload from the receipt page: (status, body). The photo lands in
    the person's own folder, named with the day on their own clock, and
    intake files it; the link is spent only on a photo or a PDF, so a wrong
    file never costs the person their link."""
    suffix = kind_of(data)
    if not suffix:
        return _take_file(desk, token, data)
    ticket, why = desk.take(token)
    if ticket is None:
        return (404 if why == "unknown" else 410), {"say": _GONE[why]}
    root = Path(tenants_root) if tenants_root else None
    cfg = load_tenant(tenant, tenants_root=root)
    folder = Path(cfg.expenses.drop_dir) / ticket.name / FUND
    folder.mkdir(parents=True, exist_ok=True)
    zone = _zone_of(tenant_dir(tenant, tenants_root=root), ticket.person) or cfg.identity.timezone
    claim = settled(ticket.claim, zone) if ticket.claim is not None else None
    tag = claim.tag() if claim is not None else "undated" if ticket.pile else "unread"
    stem = f"{local_today(zone, at=now or datetime.now(UTC))}_receipt_{tag}"
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path, n = folder / f"{stem}-{n}{suffix}", n + 1
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)  # intake never sees half a photo

    from ..engine.runner import run  # the runner loads every agent; only on an upload

    # ledger_root is this tenant's own ledger (<base>/<tenant>); the runner
    # takes the base and adds the tenant itself.
    filed = run(
        tenant, "expenses", "intake", ledger_dir=Path(ledger_root).parent, tenants_root=root
    )
    if ticket.pile:
        # A catch-up receipt waits for its own day and amount, read or said
        # later; nothing about it is guessed here.
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


def _text_of(data: bytes) -> str | None:
    """A file's text, when it's text at all."""
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _take_file(desk: ReceiptDesk, token: str, data: bytes) -> tuple[int, dict]:
    """A statement or a school list through a catch-up link (t-statements):
    read by code, held in memory for the person's word, never written; the
    link is spent on nothing, so a wrong file costs nothing."""
    text = _text_of(data)
    if text is None:
        return 415, {"say": "That isn't a photo. Tap the button and take a picture of it."}
    try:
        if statements.is_school_list(text):
            kind, rows = SCHOOL_LIST, tuple(statements.parse_school(text))
        else:
            kind, rows = STATEMENT, tuple(statements.parse(text))
    except statements.NotAStatement:
        rows = ()
    if not rows:
        return 415, {
            "say": "That file isn't a bank statement Tim can read. Send the CSV file your "
            "bank's website exports, or a photo of a receipt."
        }
    if not desk.is_pile(token):
        ticket, why = desk.take(token)
        if ticket is None:
            return (404 if why == "unknown" else 410), {"say": _GONE[why]}
        return 415, {"say": "A statement goes in through a catch-up link. Ask Tim for one."}
    ticket, _why = desk.take(token)
    desk.hold(ticket, kind, rows)
    if kind == SCHOOL_LIST:
        said = f"Got your school list: {len(rows)} payments. Tell Tim to add it."
        return 200, {"ok": True, "say": said, "note": said}
    first, last = date.fromisoformat(rows[0].day), date.fromisoformat(rows[-1].day)
    said = (
        f"Got your statement: {len(rows)} lines, {first:%b} {first.day} to {last:%b} "
        f"{last.day}. Tell Tim whose account it is: the ministry's, or your own."
    )
    return 200, {"ok": True, "say": said, "note": said}


def _unread(
    cfg: Any, data: bytes, filed: str, tenant: str, root: Path | None, ledger_root: Path
) -> str:
    """A photo that came with no amount: what it reads, as a proposal for the
    person's yes, or the question."""
    from ..engine.runner import run

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
    if length > MAX_PHOTO:
        return 413, {"say": "That photo is too large. Try again a little closer."}, True
    data = read(length) if length > 0 else b""
    token = str(headers.get("X-Receipt-Ticket") or "")
    status, body = take_photo(
        desk, token, data, tenant=tenant, tenants_root=tenants_root, ledger_root=ledger_root
    )
    return status, body, False


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


def page(nonce: str, *, many: bool = False, ministry: str = "") -> str:
    """The receipt page: one button that opens the camera; or, for a
    catch-up link (``many``), one button that picks every receipt at once
    from the camera roll or Files, lists each file by name, and sends them
    one by one on a second tap (the 10/14 phone run: the picker once sent a
    real photo by mistake). A failed send says the box's own reason. The
    link's token rides in the address's #fragment, which never reaches a
    server log."""
    title = "Send your receipts" if many else "Add your receipt"
    who = f'<span class="who">for {escape(ministry)}</span>' if ministry else ""
    if many:
        pick = (
            "<label>Choose your receipts<input id=photo type=file "
            'accept="image/*,application/pdf,.csv,text/csv" multiple></label>'
            '<ul id="files" hidden></ul><button id="send" type=button hidden></button>'
            "<p class=note>Statements or receipts in iCloud Drive? Tap the button, choose "
            "Choose File, then search for the folder by its name at the top.</p>"
        )
    else:
        pick = (
            '<label>Take the photo<input id=photo type=file accept="image/*,application/pdf"\n'
            'capture="environment"></label>'
        )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>
<style nonce="{nonce}">{_STYLE}</style></head><body>
<div class="wrap"><header class="top">{_MARK}<span><b>Tim</b>{who}</span></header><main>
<h1>{title}</h1>
{pick}
<progress id=bar max=100 value=0 hidden></progress>
<p id=status role=status></p>
</main></div>
<script nonce="{nonce}">
const many={"true" if many else "false"};
const say=t=>{{document.getElementById('status').textContent=t;}};
const bar=document.getElementById('bar'),label=document.querySelector('label');
const list=document.getElementById('files'),go=document.getElementById('send');
let chosen=[];
const send=(f,done)=>{{
const x=new XMLHttpRequest();x.open('POST','{PAGE_PATH}');x.withCredentials=false;
x.setRequestHeader('Content-Type',f.type||'application/octet-stream');
x.setRequestHeader('X-Receipt-Ticket',location.hash.slice(1));
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
+(notes.length?'\\n\\n'+notes.join('\\n\\n'):''));return;}}
const f=files[i++];bar.value=0;
say(many?`Sending ${{i}} of ${{files.length}}...`:'Sending your photo...');
send(f,(status,d)=>{{
if(status===200){{sent++;if(d.note){{notes.push(d.note);}}
if(!many){{label.hidden=true;say(d.say||'Saved.');}}}}
else{{missed++;
const why=status?(d.say||'That did not go through. Ask Tim for a new link.')
:"Your link still works: tap the button to try again.";
if(many){{if(!reasons.includes(why)){{reasons.push(why);}}}}
else{{say(status?why:"That didn't go through. "+why);}}}}
next();}});}};
next();}};
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
    "MAX_PHOTO",
    "PAGE_PATH",
    "Claim",
    "ReceiptDesk",
    "Receipts",
    "Ticket",
    "claim_in",
    "kind_of",
    "page",
    "take_photo",
    "upload",
]
