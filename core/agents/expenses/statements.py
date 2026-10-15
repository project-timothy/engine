"""Bank statements as the backbone of a catch-up (board t-statements, chunk 2,
the owner's decisions of 2026-10-14).

A missionary catching up a year has two bank accounts in the story: the
ministry's own, where every line is the ministry's, and their own, where only
the lines they pick are the ministry's and the rest is their private life.
A statement is read here, by code, from the CSV a bank exports (a Debit and
a Credit column, or one signed Amount), and nothing else:

- The ministry's account: every line is kept, as an event, never a table.
- The person's own account: Tim proposes which lines look like the
  ministry's, the person picks, and only the picked lines are ever kept.
  The rest are never written anywhere (the door holds the parsed file in
  memory for the picking, and keeps no copy of the file itself).
- Money that moves between pockets is never counted twice: a send through
  a remittance service (Remitly and the like) is money moving to where it is
  spent, and the receipts and the school list are the spending; the
  ministry paying the person back is a transfer, matched to the person's own
  statement by amount and day.
- A receipt stands behind the line it paid (its day within a few days, its
  amount the line's); for a receipt in rupees paid by card, the card's
  actual dollars on the statement are the dollars, never the day's rate.
- Scholarships and school fees come from the person's own list, a line each,
  with no receipt.

Pure functions: no I/O, no clock. The door parses and holds; the expenses
jobs record; the month reads the record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import fx, records
from .categories import canonical, from_words
from .records import LINE_EVENT, SCHOOL_EVENT, TRANSFER_EVENT
from .records import LINE_PURPOSE_EVENT as PURPOSE_EVENT
from .schema import parse_amount_tag, parse_foreign_claim

# What a kept line is, in the month (``month_lines``): spending the person
# paid and the ministry owes back, spending the ministry paid itself, money
# sent abroad (its receipts are the spending), the ministry paying the person
# back, and money in. Only the first two are spending; the rest moved.
OWED, MINISTRY_PAID, SENT, PAID_BACK, CAME_IN, REPORTED = (
    "owed back",
    "the ministry paid",
    "sent abroad",
    "paid back",
    "came in",
    "already reported",
)
SPENDING = (OWED, MINISTRY_PAID)
WAITING = "its receipt, or what it was for"
SCHOOL_CATEGORIES = ("Scholarships", "School Fees", "Education")

MINISTRY = "ministry"
OWN = "own"
ACCOUNTS = (MINISTRY, OWN)

SEND, IN, OUT = "send", "in", "out"

NEAR_DAYS = 3
"""How far apart a receipt's day and its line's day may be (a card posts a
day or two after the purchase)."""

CARD_SPREAD = Decimal("0.06")
"""How far a card's dollars may sit from the day's rate for the same rupees:
the bank's own rate and its foreign fee."""

_SENDS = re.compile(
    r"\b(remitly|wise|transferwise|western\s+union|xoom|worldremit|moneygram|ria)\b", re.I
)
_DATE_HEADS = ("date", "posted date", "posting date", "transaction date", "trans date")
_WORD_HEADS = ("description", "payee", "details", "memo", "name", "transaction")
_DEBIT_HEADS = ("debit", "debits", "withdrawal", "withdrawals", "money out", "amount debit")
_CREDIT_HEADS = ("credit", "credits", "deposit", "deposits", "money in", "amount credit")
_AMOUNT_HEADS = ("amount",)
_DAY_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y")
_SCAN_ROWS = 20  # a bank's preamble (account name, period) sits above the header


class NotAStatement(ValueError):
    """The file isn't a bank statement or a school list this code can read."""


@dataclass(frozen=True)
class Line:
    """One statement line: its day, the bank's words, and its cents (money
    out is negative). ``n`` tells apart two lines on one day for the same
    cents."""

    day: str
    words: str
    cents: int
    n: int = 0

    @property
    def kind(self) -> str:
        if _SENDS.search(self.words):
            return SEND
        return IN if self.cents > 0 else OUT

    def sha(self, person: str, account: str) -> str:
        """The line's fingerprint: the same statement sent twice is the same
        lines, never twice the money. The bank's words are left out, as in
        core.adapters.bank_csv.line_identity: banks reword memos between
        exports."""
        raw = f"bank:{person}|{account}|{self.day}|{self.cents}|{self.n}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def payload(self, person: str, account: str) -> dict:
        return {
            "sha256": self.sha(person, account),
            "person": person,
            "account": account,
            "day": self.day,
            "words": self.words,
            "cents": self.cents,
            "n": self.n,
            "kind": self.kind,
        }

    @classmethod
    def of(cls, payload: dict) -> Line:
        return cls(
            str(payload["day"]), str(payload["words"]), int(payload["cents"]), int(payload["n"])
        )


@dataclass(frozen=True)
class SchoolPayment:
    """One line of the person's school list: who, where, when, how much."""

    day: str
    student: str
    school: str
    amount: Decimal
    currency: str

    def sha(self, person: str) -> str:
        raw = (
            f"school:{person}|{self.day}|{self.student}|{self.school}|{self.amount}|{self.currency}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def payload(self, person: str) -> dict:
        return {
            "sha256": self.sha(person),
            "person": person,
            "day": self.day,
            "student": self.student,
            "school": self.school,
            "amount": str(self.amount),
            "currency": self.currency,
        }


def _rows(text: str) -> list[list[str]]:
    try:
        return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text)) if row]
    except csv.Error as exc:
        raise NotAStatement(str(exc)) from None


def _find(head: list[str], names: tuple[str, ...]) -> int | None:
    low = [h.casefold() for h in head]
    return next((low.index(n) for n in names if n in low), None)


def _header(head: list[str]) -> bool:
    """A statement's header row: a day, the bank's words, and money."""
    if _find(head, _DATE_HEADS) is None or _find(head, _WORD_HEADS) is None:
        return False
    if _find(head, _AMOUNT_HEADS) is not None:
        return True
    return _find(head, _DEBIT_HEADS) is not None and _find(head, _CREDIT_HEADS) is not None


def day_of(said: str) -> str | None:
    """A bank's day as YYYY-MM-DD, or None."""
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(said.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def cents_of(said: str) -> int | None:
    """A bank's amount in cents: "$1,234.56", "-12.00", "(12.00)"; None when
    the cell is empty or isn't money."""
    text = said.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    cents = int((value * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    return -abs(cents) if negative else cents


def parse(text: str) -> list[Line]:
    """Every line of a bank's CSV export, oldest first. Two shapes: a Debit
    and a Credit column (either may be signed or not), or one signed Amount.
    A row with no day or no amount (a running total, a footer) is skipped."""
    rows = _rows(text)
    at = next((i for i, head in enumerate(rows[:_SCAN_ROWS]) if _header(head)), None)
    if at is None:
        raise NotAStatement("no Date, Description, and Amount (or Debit and Credit) header")
    head = rows[at]
    day_col, word_col = _find(head, _DATE_HEADS), _find(head, _WORD_HEADS)
    debit, credit = _find(head, _DEBIT_HEADS), _find(head, _CREDIT_HEADS)
    amount = _find(head, _AMOUNT_HEADS)
    seen: dict[tuple[str, int], int] = {}
    out = []
    for row in rows[at + 1 :]:

        def cell(i: int | None, row: list[str] = row) -> str:
            return row[i] if i is not None and i < len(row) else ""

        day = day_of(cell(day_col))
        if day is None:
            continue
        if amount is not None:
            cents = cents_of(cell(amount))
        else:
            out_c, in_c = cents_of(cell(debit)), cents_of(cell(credit))
            cents = -abs(out_c) if out_c else abs(in_c) if in_c else None
        if not cents:
            continue
        words = " ".join(cell(word_col).split())
        n = seen.get((day, cents), 0)
        seen[(day, cents)] = n + 1
        out.append(Line(day, words, cents, n))
    if not out:
        raise NotAStatement("a statement header, but no lines with a day and an amount")
    return sorted(out, key=lambda ln: (ln.day, ln.n))


def parse_school(text: str) -> list[SchoolPayment]:
    """The person's school list: student, amount, date, school, and an
    optional currency (dollars when it has none)."""
    rows = _rows(text)
    if not rows:
        raise NotAStatement("an empty list")
    head = [h.casefold() for h in rows[0]]
    need = ("student", "amount", "date", "school")
    if not all(n in head for n in need):
        raise NotAStatement("a school list needs student, amount, date, and school columns")
    col = {n: head.index(n) for n in (*need, "currency") if n in head}
    out = []
    for row in rows[1:]:
        cells = {n: row[i] if i < len(row) else "" for n, i in col.items()}
        day = day_of(cells["date"])
        cents = cents_of(cells["amount"])
        if day is None or not cents or not cells["student"].strip():
            continue
        out.append(
            SchoolPayment(
                day=day,
                student=" ".join(cells["student"].split()),
                school=" ".join(cells["school"].split()),
                amount=Decimal(abs(cents)) / 100,
                currency=(cells.get("currency") or "USD").strip().upper() or "USD",
            )
        )
    return out


def is_school_list(text: str) -> bool:
    head = _rows(text)[:1]
    return bool(head) and {"student", "school"} <= {h.casefold() for h in head[0]}


def looks_like(line: Line, categories: list[str]) -> str:
    """Why a line on the person's own statement looks like the ministry's,
    in a few words, or "": Tim's proposal, never the person's pick."""
    if line.kind == SEND:
        return "money sent abroad"
    if line.kind == OUT:
        category = from_words(line.words, categories)
        if category:
            return category
    return ""


def _days_apart(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def match_transfers(ministry: list[Line], own: list[Line]) -> list[tuple[Line, Line]]:
    """The ministry paying the person back: a line out of the ministry's
    account and the same dollars into the person's own within a few days,
    each line matched once, the nearest day first."""
    pairs, used = [], set()
    for m in [ln for ln in ministry if ln.kind == OUT]:
        near = [
            o
            for o in own
            if o.kind == IN
            and o.cents == -m.cents
            and _days_apart(o.day, m.day) <= NEAR_DAYS
            and o not in used
        ]
        if near:
            o = min(near, key=lambda o: _days_apart(o.day, m.day))
            used.add(o)
            pairs.append((m, o))
    return pairs


@dataclass(frozen=True)
class Receipt:
    """What a receipt says for matching: its day and its dollars, either as
    paid in dollars or as its rupees at the day's rate (an estimate only:
    the card's dollars win)."""

    sha: str
    day: str
    cents: int
    foreign: bool = False


def attach(receipts: list[Receipt], lines: dict[str, Line]) -> dict[str, str]:
    """Which line each receipt stands behind (receipt sha to line sha): a
    line of money out, its day within a few days of the receipt's; in
    dollars, the same cents; in another currency, the card's dollars within
    the bank's spread of the day's rate. Each line takes one receipt; the
    closest match wins."""
    candidates = []
    for r in receipts:
        for sha, ln in lines.items():
            if ln.kind != OUT or _days_apart(ln.day, r.day) > NEAR_DAYS:
                continue
            paid = -ln.cents
            if r.foreign:
                off = abs(Decimal(paid - r.cents)) / Decimal(max(r.cents, 1))
                if off > CARD_SPREAD:
                    continue
            elif paid != r.cents:
                continue
            candidates.append((abs(paid - r.cents), _days_apart(ln.day, r.day), r.sha, sha))
    out: dict[str, str] = {}
    taken: set[str] = set()
    for _gap, _days, receipt, line in sorted(candidates):
        if receipt in out or line in taken:
            continue
        out[receipt] = line
        taken.add(line)
    return out


def school_name(row: dict) -> str:
    """A school payment as a door receipt's name, so the month prices it
    the one way a foreign amount is priced (fx.priced): at its day's rate."""
    amount = Decimal(str(row["amount"]))
    currency = str(row.get("currency") or "USD")
    tag = f"${amount:.2f}" if currency == "USD" else f"{currency}{amount:.2f}"
    return f"{row['day']}_receipt_{tag}.school"


def receipts_in(log: list[dict], rates: dict) -> tuple[list[Receipt], dict[str, dict]]:
    """Every door receipt with a day and an amount, for matching to lines,
    and each one's landed payload. The amount is the person's word, else the
    photo's reading, else what the name carries; one in another currency is
    estimated at its day's rate, and only when that rate is recorded."""
    dated = records.days(log)
    said: dict[str, str] = {}
    read: dict[str, str] = {}
    landed: dict[str, dict] = {}
    gone: set[str] = set()
    for e in log:
        kind, payload = e.get("event_type"), e.get("payload", {})
        sha = str(payload.get("sha256") or "")
        if not sha:
            continue
        if kind == records.LANDED_EVENT:
            landed[sha] = payload
        elif kind == records.AMOUNT_EVENT and payload.get("tag"):
            said[sha] = str(payload["tag"])
        elif kind == records.READ_EVENT and payload.get("tag"):
            read[sha] = str(payload["tag"])
        elif kind == records.REFERENCE_EVENT:
            gone.add(sha)
    out = []
    for sha, payload in landed.items():
        name = str(payload.get("file", ""))
        if sha in gone or not re.match(r"^\d{4}-\d{2}-\d{2}_receipt_", name):
            continue
        day = dated.get(sha) or ("" if records.UNDATED in name else name[:10])
        if not day:
            continue
        tag = said.get(sha) or read.get(sha) or ""
        if not tag:
            foreign = parse_foreign_claim(name)
            cents = parse_amount_tag(name)
            tag = (
                f"{foreign[2]}{foreign[1]:.2f}"
                if foreign
                else f"${Decimal(cents) / 100:.2f}"
                if cents
                else ""
            )
        if tag.startswith("$"):
            out.append(Receipt(sha, day, int(Decimal(tag[1:]) * 100)))
        elif tag and (rate := rates.get((tag[:3], day))) is not None:
            out.append(Receipt(sha, day, rate.cents(Decimal(tag[3:])), foreign=True))
    return out, landed


def month_lines(
    log: list[dict],
    month: str,
    *,
    reported: set[str] | frozenset[str] = frozenset(),
    categories: list[str] | tuple[str, ...] = (),
) -> list[dict]:
    """Every kept statement line and school payment in ``month``, each with
    what it is (``role``), its dollars, the receipt standing behind it, its
    category, and what it still waits on. ``reported`` are receipts and lines
    already in a report. Read only: the month job and month_report both read
    the books through here, so the card and the walk say the same thing."""
    rates = fx.stored(log)
    kept: dict[str, dict] = {}
    moved: set[str] = set()
    purpose: dict[str, dict] = {}
    placed: dict[str, str] = {}
    reading_cat: dict[str, str] = {}
    school: dict[str, dict] = {}
    for e in log:
        kind, payload = e.get("event_type"), e.get("payload", {})
        sha = str(payload.get("sha256") or "")
        if kind == LINE_EVENT:
            kept.setdefault(sha, payload)
        elif kind == TRANSFER_EVENT:
            moved.add(sha)
        elif kind == PURPOSE_EVENT:
            purpose[sha] = payload
        elif kind == records.CATEGORY_EVENT:
            placed[sha] = str(payload.get("category", ""))
        elif kind == records.READ_EVENT and payload.get("category"):
            reading_cat[sha] = str(payload["category"])
        elif kind == SCHOOL_EVENT:
            school.setdefault(sha, payload)
    receipts, landed = receipts_in(log, rates)
    out_lines = {
        sha: Line.of(p) for sha, p in kept.items() if Line.of(p).kind == OUT and sha not in moved
    }
    behind = {line: receipt for receipt, line in attach(receipts, out_lines).items()}
    out: list[dict] = []
    for sha, p in sorted(kept.items(), key=lambda kv: (kv[1]["day"], kv[1]["n"])):
        if str(p["day"])[:7] != month:
            continue
        line = Line.of(p)
        mine = p["account"] == OWN
        role = (
            SENT
            if line.kind == SEND
            else PAID_BACK
            if sha in moved or (mine and line.kind == IN)
            else CAME_IN
            if line.kind == IN
            else OWED
            if mine
            else MINISTRY_PAID
        )
        receipt = behind.get(sha, "")
        if receipt in reported:
            role = REPORTED  # its receipt is already in a report: never twice the money
        said = purpose.get(sha, {})
        looks = str(p.get("looks", ""))
        category = (
            placed.get(sha)
            or said.get("category")
            or placed.get(receipt, "")
            or reading_cat.get(receipt, "")
            or (canonical(looks, list(categories)) or "" if looks else "")
        )
        explained = bool(receipt or said.get("said") or category)
        out.append(
            {
                "sha256": sha,
                "person": str(p["person"]),
                "account": str(p["account"]),
                "day": line.day,
                "words": line.words,
                "cents": abs(line.cents),
                "role": role,
                "receipt": receipt,
                "receipt_file": str(landed.get(receipt, {}).get("filed_to", "")),
                "category": category,
                "said": str(said.get("said", "")),
                "waiting_on": WAITING if role in SPENDING and not explained else "",
                "reported": sha in reported,
            }
        )
    listed = list(categories)
    for sha, p in sorted(school.items(), key=lambda kv: kv[1]["day"]):
        if str(p["day"])[:7] != month:
            continue
        name = school_name(p)
        fields = fx.priced(name, rates)
        cents = fields.get("amount_cents") if fields else parse_amount_tag(name)
        out.append(
            {
                "sha256": sha,
                "person": str(p["person"]),
                "account": "",
                "day": str(p["day"]),
                "words": f"{p['student']}, {p['school']}",
                "cents": cents,
                "role": OWED,
                "school": True,
                "claim_name": name,
                "receipt": "",
                "receipt_file": "",
                "category": placed.get(sha)
                or next((c for n in SCHOOL_CATEGORIES if (c := canonical(n, listed))), ""),
                "said": "",
                "waiting_on": "the day's exchange rate" if cents is None else "",
                "reported": sha in reported,
                **{k: v for k, v in fields.items() if k.startswith(("said_", "fx_"))},
            }
        )
    return out


def attached(log: list[dict]) -> set[str]:
    """Receipts standing behind a kept line: the line carries them, and they
    are never a line of their own."""
    rates = fx.stored(log)
    receipts, _landed = receipts_in(log, rates)
    kept = {}
    moved = set()
    for e in log:
        payload = e.get("payload", {})
        if e.get("event_type") == LINE_EVENT:
            kept.setdefault(str(payload["sha256"]), payload)
        elif e.get("event_type") == TRANSFER_EVENT:
            moved.add(str(payload.get("sha256")))
    lines = {s: Line.of(p) for s, p in kept.items() if Line.of(p).kind == OUT and s not in moved}
    return set(attach(receipts, lines))


def moved_words(lines: list[dict]) -> str:
    """The month's money that moved but wasn't spending, for the card: what
    was sent abroad, paid back, and what came in."""
    parts = []
    for ln in lines:
        if ln["role"] in (SENT, PAID_BACK, CAME_IN):
            parts.append(f"{ln['day']}: ${ln['cents'] / 100:,.2f} {ln['role']} ({ln['words']})")
    return "; ".join(parts)


__all__ = [
    "ACCOUNTS",
    "IN",
    "LINE_EVENT",
    "MINISTRY",
    "OUT",
    "OWN",
    "SCHOOL_EVENT",
    "SEND",
    "TRANSFER_EVENT",
    "Line",
    "NotAStatement",
    "Receipt",
    "SchoolPayment",
    "attach",
    "is_school_list",
    "looks_like",
    "match_transfers",
    "parse",
    "parse_school",
]
