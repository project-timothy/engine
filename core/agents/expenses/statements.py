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
- Cash from an ATM is money moving too (the owner's decision of 2026-10-15):
  the withdrawal turns dollars into rupees in hand, at what they actually
  cost (the dollars debited over the rupees the slip says it gave). Rupee
  receipts and rupee school payments spend that cash down, oldest cash
  first, at its cost, never at the day's rate; what's left is cash on hand.
  The bank's ATM fee is its own line, spending like any other.
- The cash is counted (board t-cash-count, 2026-10-15): the person says
  what's in hand; whatever the books hold above it went with no receipt,
  to the ministry's spending (tea, taxis, tips) or to the person's own
  use, and they say which. A receipt that was partly their own is split
  (t-split-receipt): only the ministry's part counts, and the person's part
  of ministry money is owed to the ministry. Cash not yet accounted for
  ages (t-advance-clock): a nudge past 45 days, the 60-day mark, and the
  reviewer hears past 120.

Pure functions: no I/O, no clock. The door parses and holds; the expenses
jobs record; the month reads the record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ...policy import Clock
from . import fx, records
from .categories import canonical, from_words
from .records import (
    CASH_COUNT_EVENT,
    CASH_USED_EVENT,
    LINE_EVENT,
    LINE_SPLIT_EVENT,
    RECEIPT_SPLIT_EVENT,
    RODE_EVENT,
    SCHOOL_EVENT,
    TRANSFER_EVENT,
)
from .records import LINE_PURPOSE_EVENT as PURPOSE_EVENT
from .schema import parse_amount_tag, parse_foreign_claim

# What a kept line is, in the month (``month_lines``): spending the person
# paid and the ministry owes back, spending the ministry paid itself, money
# sent abroad (its receipts are the spending), the ministry paying the person
# back, and money in. Only the first two are spending; the rest moved.
OWED, MINISTRY_PAID, SENT, PAID_BACK, CAME_IN, REPORTED, CASH, REPAY = (
    "owed back",
    "the ministry paid",
    "sent abroad",
    "paid back",
    "came in",
    "already reported",
    "cash drawn",
    "owed to the ministry",
)
THEIRS_IN = "their own money in"
MOVED = "moved between its accounts"
FROM_PAY = "paid from their pay"
PAY_OUT = "their pay"
"""A payment out of the ministry's account that was a person's pay under the
board's resolution (c34): the ministry's spending under its Pay kind, never
owed back, and on that person's 1099 total."""
PAY_IN = "their pay received"
"""A deposit on a person's own account that is a ``their pay`` line of the
ministry arriving: the same person, the same dollars, within a few days
(engine #651, the owner's call of 2026-11-16). The same money as the pay
line, never the ministry paying them back, and never counted again."""
PAY_KIND = "Pay"
"""The ministry's kind for pay, where its own list doesn't spell it otherwise."""
"""A ministry cost on the person's own account that they paid from their own
pay, with no one paying them back (c33): never owed back, never the
ministry's spending, and a cost on their tax packet's Schedule C."""
"""Money the ministry moved from one of its own accounts to another: a
payment service's payout and the bank deposit it became (c32). Never
income, never spending; the deposit carries the move in the books."""
"""Money in on the ministry's account that was the person's own: their pay
from a support platform landing there, or a gift given for them (c31, the
EquipNet lesson of 2026-11-12). It credits their fund, never the ministry's
income or its gross receipts."""
SPENDING = (OWED, MINISTRY_PAID)
WAITING = "its receipt, or what it was for"
WAITING_CASH = "how many rupees it gave (the ATM slip says)"
WAITING_WHICH = "which of their receipts it paid (Tim asks)"
BANK_FEES = ("Bank Fees", "Other")
"""Where the bank's own ATM fee reads, the first the ministry lists."""
SCHOOL_CATEGORIES = ("Scholarships", "School Fees", "Education")
CASH_CATEGORIES = ("Other",)
"""Where cash with no receipt reads when its words name no category."""
OWN_PART = ":own"
"""A receipt's or a cash gap's sha with this added: the person's own part."""

MINISTRY = "ministry"
OWN = "own"
ACCOUNTS = (MINISTRY, OWN)
PAY = "pay"
"""What a person says of a line on their own account paid from their pay (c33)."""

SEND, IN, OUT, ATM = "send", "in", "out", "atm"

BANK, CASH_IN_HAND, PERSON, PROCESSOR = "bank", "cash", "person", "processor"
"""Where a row's money came from (``paid_from``): the ministry's bank account,
its ATM cash in hand, the person (their own money, or ministry money they
already held), or a payment service the ministry takes gifts through (the
row names which, processors.py). The double-entry books read it (journal.py)."""

NEAR_DAYS = 3
"""How far apart a receipt's day and its line's day may be (a card posts a
day or two after the purchase)."""

CARD_SPREAD = Decimal("0.06")
"""How far a card's dollars may sit from the day's rate for the same rupees:
the bank's own rate and its foreign fee."""

_SENDS = re.compile(
    r"\b(remitly|wise|transferwise|western\s+union|xoom|worldremit|moneygram|ria)\b", re.I
)
_ATM = re.compile(r"\batm\b|\bcash\s+(withdrawal|withdraw|advance)\b", re.I)
_FEE = re.compile(r"\bfees?\b", re.I)
_FOREIGN = re.compile(r"\b(foreign|intl|international|exch\w*|non-\w+)\b", re.I)
_DATE_HEADS = ("date", "transaction date", "trans date", "posted date", "posting date")
"""The day a line happened before the day the bank posted it: an export with
both columns is read by its transaction date."""
_WORD_DAY = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
"""A day inside the bank's words ("NON-CHASE ATM WITHDRAW 000123 10/03")."""
WORD_DAY_REACH = 7
"""How far before its posting a day in the bank's words may fall."""
_WORD_HEADS = ("description", "payee", "details", "memo", "name", "transaction")
_DEBIT_HEADS = ("debit", "debits", "withdrawal", "withdrawals", "money out", "amount debit")
_CREDIT_HEADS = ("credit", "credits", "deposit", "deposits", "money in", "amount credit")
_AMOUNT_HEADS = ("amount",)
_BALANCE_HEADS = ("balance", "running balance", "ledger balance", "available balance")
"""The bank's own balance after each line, when the export carries one."""
_DAY_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y")
_NAMES = re.compile(
    r"\bind\s+name\s*:\s*([a-z][a-z .'-]*?)\s*(?=\b(?:trn|ind\s+id|id|co|sec|descr)\s*:|$)", re.I
)
"""A payroll deposit's individual name (an ACH PPD line's ``Ind Name:``)."""
_SELF = ("me", "myself", "mine", "own", "my own", "i did", "i paid")
_SCAN_ROWS = 20  # a bank's preamble (account name, period) sits above the header


class NotAStatement(ValueError):
    """The file isn't a bank statement or a school list this code can read.
    ``say`` is the reason in the person's terms, when there is one to give."""

    def __init__(self, why: str, say: str = "") -> None:
        super().__init__(why)
        self.say = say


_MONEY = re.compile(r"[-+]?\(?[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\)?")
"""An amount this code reads: thousands by commas, a point for the cents, a
sign in front or parentheses. "6,80" and "6.80-" are not guessed at."""
_LOOKS_LIKE_A_DAY = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}")


@dataclass(frozen=True)
class Line:
    """One statement line: its day, the bank's words, and its cents (money
    out is negative). ``n`` tells apart two lines on one day for the same
    cents. ``day_end`` is the bank's own balance at the end of the line's
    day, on the day's last line only, when the export has a Balance column
    (core/agents/expenses/balances.py); it is never part of the line."""

    day: str
    words: str
    cents: int
    n: int = 0
    day_end: int | None = field(default=None, compare=False)

    @property
    def kind(self) -> str:
        if _SENDS.search(self.words):
            return SEND
        if self.cents < 0 and _ATM.search(self.words) and not _FEE.search(self.words):
            return ATM
        return IN if self.cents > 0 else OUT

    @property
    def bank_fee(self) -> bool:
        """The bank's own fee for an ATM or a foreign purchase: spending,
        never waiting on what it was for."""
        return (
            self.cents < 0
            and bool(_FEE.search(self.words))
            and bool(_ATM.search(self.words) or _FOREIGN.search(self.words))
        )

    @property
    def drawn_on(self) -> str | None:
        """The day the bank's own words carry, when it falls within a week
        before the posting: an ATM in Kathmandu on a Saturday night posts at
        a US bank on Monday, and the cash was in hand on Saturday."""
        posted = date.fromisoformat(self.day)
        for month, day in _WORD_DAY.findall(self.words):
            for year in (posted.year, posted.year - 1):
                try:
                    said = date(year, int(month), int(day))
                except ValueError:
                    break
                if 0 <= (posted - said).days <= WORD_DAY_REACH:
                    return said.isoformat()
        return None

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
    paid_by: str = ""  # OWN when the list says the person paid it themselves

    def sha(self, person: str) -> str:
        raw = (
            f"school:{person}|{self.day}|{self.student}|{self.school}|{self.amount}|{self.currency}"
        )
        if self.paid_by:  # a row nobody said they paid keeps the sha it always had
            raw += f"|{self.paid_by}"
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
            **({"paid_by": self.paid_by} if self.paid_by else {}),
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


def day_words(day: str) -> str:
    """A day as the person hears it: "Sep 2, 2026"."""
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}, {d.year}"


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
    text = said.strip().replace("$", "").replace(" ", "")
    if not _MONEY.fullmatch(text):
        return None
    text = text.replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    cents = int((value * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    return -abs(cents) if negative else cents


def _money(cell: str) -> int | None:
    """A dated row's amount in cents, None for an empty cell; a cell that
    holds something this code cannot read refuses the whole statement, since
    skipping the row would leave money out of the books without a word."""
    if not cell.strip():
        return None
    cents = cents_of(cell)
    if cents is None:
        raise NotAStatement(
            f"an amount this code cannot read: {cell!r}",
            f"Tim can't read the amount {cell} in that file. Export it again with amounts "
            "written like 1,234.56 and a minus sign in front of money going out.",
        )
    return cents


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
    balance = _find(head, _BALANCE_HEADS)
    seen: dict[tuple[str, int], int] = {}
    out = []
    said: list[int | None] = []  # the bank's balance after each line, in the file's order
    for row in rows[at + 1 :]:

        def cell(i: int | None, row: list[str] = row) -> str:
            return row[i] if i is not None and i < len(row) else ""

        day = day_of(cell(day_col))
        if day is None:
            if _LOOKS_LIKE_A_DAY.fullmatch(cell(day_col)):
                raise NotAStatement(
                    f"a day this code cannot read: {cell(day_col)!r}",
                    f"Tim can't tell the day from {cell(day_col)} in that file. Export it "
                    "again with dates written month first (08/25/2026) or as 2026-08-25.",
                )
            continue  # a running total, a footer
        if amount is not None:
            cents = _money(cell(amount))
        else:
            out_c, in_c = _money(cell(debit)), _money(cell(credit))
            cents = -abs(out_c) if out_c else abs(in_c) if in_c else None
        if not cents:
            continue
        words = " ".join(cell(word_col).split())
        n = seen.get((day, cents), 0)
        seen[(day, cents)] = n + 1
        out.append(Line(day, words, cents, n))
        said.append(cents_of(cell(balance)) if balance is not None else None)
    if not out:
        raise NotAStatement("a statement header, but no lines with a day and an amount")
    if amount is not None and all(ln.cents > 0 for ln in out):
        # A card's export lists charges as positive: read as-is, each is money in.
        raise NotAStatement(
            "one Amount column and nothing negative",
            "Every amount in that file is positive, so Tim can't tell money out from "
            "money in. Export it again with separate Debit and Credit columns, or with a "
            "minus sign on money going out.",
        )
    return sorted(_day_ends(out, said), key=lambda ln: (ln.day, ln.n))


def _day_ends(lines: list[Line], said: list[int | None]) -> list[Line]:
    """Each day's closing balance on its last line, in the file's order.
    A bank lists oldest first or newest first (Chase: newest); the order is
    the one the balances themselves follow (each one the last plus the
    line's cents), and the days decide when the arithmetic can't. A blank
    or unreadable balance on a day's last line leaves that day without
    one: a balance is never guessed."""
    if not any(b is not None for b in said):
        return lines
    order = list(range(len(lines)))
    if _follows(lines, said, order[::-1]) > _follows(lines, said, order) or (
        _follows(lines, said, order[::-1]) == _follows(lines, said, order)
        and lines[0].day > lines[-1].day
    ):
        order.reverse()
    last: dict[str, int] = {lines[i].day: i for i in order}
    return [
        replace(ln, day_end=said[i]) if last[ln.day] == i and said[i] is not None else ln
        for i, ln in enumerate(lines)
    ]


def _follows(lines: list[Line], said: list[int | None], order: list[int]) -> int:
    """How many lines' balances follow from the one before, in this order."""
    return sum(
        1
        for a, b in zip(order, order[1:], strict=False)
        if said[a] is not None and said[b] is not None and said[a] + lines[b].cents == said[b]
    )


def parse_school(text: str) -> list[SchoolPayment]:
    """The person's school list: student, amount, date, school, an optional
    currency (dollars when it has none), and an optional "Paid by" where
    "me" (or "myself", "mine", "own") says the person paid it themselves;
    anything else is the ministry's money (board w-school-cash)."""
    rows = _rows(text)
    if not rows:
        raise NotAStatement("an empty list")
    head = [h.casefold() for h in rows[0]]
    need = ("student", "amount", "date", "school")
    if not all(n in head for n in need):
        raise NotAStatement("a school list needs student, amount, date, and school columns")
    col = {n: head.index(n) for n in (*need, "currency", "paid by") if n in head}
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
                paid_by=OWN if cells.get("paid by", "").strip().casefold() in _SELF else "",
            )
        )
    return out


def is_school_list(text: str) -> bool:
    head = _rows(text)[:1]
    return bool(head) and {"student", "school"} <= {h.casefold() for h in head[0]}


def names_a_person(words: str) -> str:
    """The person a deposit's words name (a payroll deposit's individual
    name), or nothing: a deposit naming a person is likely that person's pay
    (the EquipNet lesson, 2026-11-12), so Tim asks, never assumes."""
    found = _NAMES.search(words)
    return found[1].strip() if found else ""


def looks_like(line: Line, categories: list[str]) -> str:
    """Why a line on the person's own statement looks like the ministry's,
    in a few words, or "": Tim's proposal, never the person's pick."""
    if line.kind == SEND:
        return "money sent abroad"
    if line.kind == ATM:
        return "cash drawn"
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
    person: str = ""


def attach(
    receipts: list[Receipt],
    lines: dict[str, Line],
    whose: dict[str, str] | None = None,
    said: dict[str, str] | None = None,
) -> dict[str, str]:
    """Which line each receipt stands behind (receipt sha to line sha); the
    rules are ``seat``'s."""
    return seat(receipts, lines, whose, said)[0]


def seat(
    receipts: list[Receipt],
    lines: dict[str, Line],
    whose: dict[str, str] | None = None,
    said: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Which line each receipt stands behind (receipt sha to line sha), and
    the lines in doubt (line sha to the receipts that could each be it). A
    line of money out, its day within a few days of the receipt's; in
    dollars, the same cents; in another currency, the card's dollars within
    the bank's spread of the day's rate. A receipt stands behind its own
    person's line (``whose``: line sha to person), never the bank's own fee
    (found 2026-10-15: a 700-rupee receipt took a $5 ATM fee). Each line
    takes one receipt, as
    many receipts as can find a line do, and among those the closest match
    wins. Two of a person's receipts in another currency that could each be
    a line, one left over, can't be told apart by their amounts (the
    fuzzer, 2026-10-15): neither is guessed, the line is in doubt until the
    person says which it carried (``said``: receipt sha to line sha, from
    ``rode``), and a receipt they named rides its line first."""
    whose = whose or {}
    said = {r: ln for r, ln in (said or {}).items() if ln in lines}
    foreign = {r.sha for r in receipts if r.foreign}
    candidates = []
    for r in receipts:
        for sha, ln in lines.items():
            if ln.kind != OUT or ln.bank_fee or _days_apart(ln.day, r.day) > NEAR_DAYS:
                continue
            if r.person and whose.get(sha, r.person) != r.person:
                continue
            paid = -ln.cents
            if r.foreign:
                off = abs(Decimal(paid - r.cents)) / Decimal(max(r.cents, 1))
                if off > CARD_SPREAD:
                    continue
            elif paid != r.cents:
                continue
            candidates.append((abs(paid - r.cents), _days_apart(ln.day, r.day), r.sha, sha))
    near: dict[str, list[str]] = {}
    for _gap, _days, receipt, line in sorted(candidates):
        if receipt not in said:  # one they named rides only the line they named
            near.setdefault(receipt, []).append(line)
    holder: dict[str, str] = {line: receipt for receipt, line in said.items()}
    fixed = set(holder)

    def place(receipt: str, tried: set[str]) -> bool:
        """Give ``receipt`` its closest free line, or move the receipt
        holding one to another of its own (found 2026-10-15: two rupee
        purchases days apart took each other's line, and the receipt left
        over read as cash)."""
        for line in near[receipt]:
            if line in tried or line in fixed:
                continue
            tried.add(line)
            if line not in holder or place(holder[line], tried):
                holder[line] = receipt
                return True
        return False

    for receipt in near:
        place(receipt, set())
    seated = set(holder.values())
    doubt: dict[str, list[str]] = {}
    for line, receipt in holder.items():
        if line in fixed or receipt not in foreign:
            continue
        rivals = [r for r in near if r in foreign and r not in seated and line in near[r]]
        if rivals:
            doubt[line] = sorted([receipt, *rivals])
    attached = {receipt: line for line, receipt in holder.items() if line not in doubt}
    return attached, doubt


def school_name(row: dict) -> str:
    """A school payment as a door receipt's name, so the month prices it
    the one way a foreign amount is priced (fx.priced): at its day's rate."""
    amount = Decimal(str(row["amount"]))
    currency = str(row.get("currency") or "USD")
    tag = f"${amount:.2f}" if currency == "USD" else f"{currency}{amount:.2f}"
    return f"{row['day']}_receipt_{tag}.school"


def _receipt_tags(log: list[dict]) -> tuple[list[tuple[str, str, str, str]], dict[str, dict]]:
    """Every door receipt with a day and an amount as (sha, day, tag,
    person), and each one's landed payload. The amount is the person's word,
    else the photo's reading, else what the name carries ("$43.00",
    "NPR4300.00")."""
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
        if tag:
            out.append((sha, day, tag, str(payload.get("person", ""))))
    return out, landed


def receipts_in(log: list[dict], rates: dict) -> tuple[list[Receipt], dict[str, dict]]:
    """Every door receipt with a day and an amount, for matching to lines,
    and each one's landed payload. One in another currency is estimated at
    its day's rate, and only when that rate is recorded."""
    tags, landed = _receipt_tags(log)
    own = splits(log)
    out = []
    for sha, day, tag, person in tags:
        tag = plus_tag(tag, own.get(sha, ""))  # a line carries the whole receipt, both parts
        if tag.startswith("$"):
            out.append(Receipt(sha, day, int(Decimal(tag[1:]) * 100), person=person))
        elif (rate := rates.get((tag[:3], day))) is not None:
            cents = rate.cents(Decimal(tag[3:]))
            out.append(Receipt(sha, day, cents, foreign=True, person=person))
    return out, landed


def kept_receipts(log: list[dict]) -> list[tuple[str, str, str, str]]:
    """Every door receipt Tim keeps with a day and an amount, as (sha, day,
    tag, person)."""
    return _receipt_tags(log)[0]


def foreign_receipts(log: list[dict]) -> list[tuple[str, str, str, Decimal]]:
    """Every receipt in another currency that has its day and its amount, as
    (sha, day, currency, amount): what needs that day's rate."""
    out = []
    for sha, day, tag, _person in _receipt_tags(log)[0]:
        if (got := _foreign(tag)) is not None:
            out.append((sha, day, *got))
    return out


def _foreign(tag: str) -> tuple[str, Decimal] | None:
    """("NPR", 40000) from "NPR40000.00"; None for dollars or anything else."""
    found = re.fullmatch(r"([A-Z]{3})(\d+(?:\.\d+)?)", tag)
    if not found or found[1] == "USD":
        return None
    return found[1], Decimal(found[2])


def cash_words(tag: str) -> str:
    """ "NPR 40,000.00" from "NPR40000.00"; "" for anything else."""
    got = _foreign(tag)
    return f"{got[0]} {got[1]:,.2f}" if got else ""


def amount_of(tag: str) -> tuple[str, Decimal] | None:
    """("USD", 43) from "$43.00", ("NPR", 4300) from "NPR4300.00"."""
    if tag.startswith("$"):
        try:
            return "USD", Decimal(tag[1:])
        except InvalidOperation:
            return None
    return _foreign(tag)


def plus_tag(tag: str, more: str) -> str:
    """Two amounts in one currency added, in ``tag``'s form; ``tag`` alone
    when ``more`` is empty or another currency."""
    a, b = amount_of(tag), amount_of(more)
    if a is None or b is None or a[0] != b[0]:
        return tag
    total = a[1] + b[1]
    return f"${total:.2f}" if a[0] == "USD" else f"{a[0]}{total:.2f}"


def splits(log: list[dict]) -> dict[str, str]:
    """Each split receipt's own part, as a tag (the latest said)."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"].get("own", ""))
        for e in log
        if e.get("event_type") == RECEIPT_SPLIT_EVENT and e.get("payload", {}).get("sha256")
    }


def _once(log: list[dict], kind: str) -> list[dict]:
    """Each event of ``kind`` once, by its sha, in the order first seen."""
    out: dict[str, dict] = {}
    for e in log:
        if e.get("event_type") == kind:
            out.setdefault(str(e["payload"].get("sha256")), dict(e["payload"]))
    return list(out.values())


def counts(log: list[dict]) -> list[dict]:
    """Every cash count, oldest first: who, the day, and the tag counted."""
    return _once(log, CASH_COUNT_EVENT)


def _told(log: list[dict], kind: str) -> dict[str, dict]:
    """Who said each ``kind`` event's words and when, by its sha: the door's
    id of the person and the moment the ledger took it (persona night S05,
    2026-11-16: a line said earlier that day had no answer to "who logged
    this?")."""
    out: dict[str, dict] = {}
    for e in log:
        if e.get("event_type") == kind and e.get("created_at"):
            sha = str(e["payload"].get("sha256"))
            by = str(e["payload"].get("by") or "")
            out.setdefault(sha, {"told_by": by, "told_at": str(e["created_at"])})
    return out


def used(log: list[dict]) -> list[dict]:
    """Cash gone with no receipt, as said: whose use, the day, the tag."""
    return _once(log, CASH_USED_EVENT)


def used_name(row: dict) -> str:
    """Cash used with no receipt as a door receipt's name, priced the one
    way a foreign amount is (fx.priced) when no cash stands under it."""
    return f"{row['day']}_receipt_{row['tag']}.cash"


@dataclass
class _Cash:
    """One ATM withdrawal: the rupees it gave, the dollars they cost, and
    how many rupees are still in hand. ``day`` is when it was drawn, as the
    bank's words say, else the day it posted; ``usable`` is the first day it
    can have paid for anything: the day it was drawn, or, when the bank
    doesn't say, a few days before it posted."""

    day: str
    usable: str
    rupees: Decimal
    cents: int
    ministry: bool
    left: Decimal

    def cost(self, rupees: Decimal) -> Decimal:
        return Decimal(self.cents) * rupees / self.rupees


def cash(
    log: list[dict], *, through: str = ""
) -> tuple[dict[str, fx.Drawn], dict[tuple[str, str], tuple[Decimal, int]]]:
    """What ATM cash paid for (each spend's ``fx.Drawn``), and what's left
    in hand by (person, currency): rupees and their cost in cents. The
    rules are ``_cash_state``'s."""
    drawn, pools = _cash_state(log, through)
    on_hand = {}
    for key, pool in pools.items():
        held = [c for c in pool if not through or c.usable <= through]
        rupees = sum((c.left for c in held), Decimal(0))
        if rupees:
            cost = sum((c.cost(c.left) for c in held), Decimal(0))
            on_hand[key] = (rupees, int(cost.quantize(Decimal(1), ROUND_HALF_UP)))
    return drawn, on_hand


def cash_age(log: list[dict], today: str) -> dict[tuple[str, str], tuple[str, Decimal]]:
    """The oldest cash each person still holds, by (person, currency): the
    day it was drawn and how much of it is left (t-advance-clock). Only the
    ministry's cash grows old: it is spent first, oldest first, so only what
    nobody has accounted for is left, and the person's own is their own."""
    _drawn, pools = _cash_state(log, today)
    out = {}
    for key, pool in pools.items():
        left = [c for c in pool if c.left > 0 and c.usable <= today and c.ministry]
        if left:
            out[key] = (left[0].day, left[0].left)
    return out


def days_old(day: str, today: str) -> int:
    return (date.fromisoformat(today) - date.fromisoformat(day)).days


def _cash_state(log: list[dict], through: str) -> tuple[dict[str, fx.Drawn], dict]:
    """What ATM cash paid for, and what's left (the owner's decision of
    2026-10-15). Each foreign receipt that no card line carries, and each
    foreign school payment, spends the person's cash in its currency drawn
    on or before its day, at what that cash cost: the ministry's cash first,
    oldest first, then the person's own (the owner, 2026-10-16), whose part
    is owed back to them (``fx.Drawn.own_cents``). Cash is
    drawn on the day the bank's words say, else up to a few days before it
    posted (a bank posts a weekend withdrawal abroad days late). Cash on hand
    is the rupees not yet spent through ``through`` (a day; "" = all), with
    their cost in cents, by (person, currency). A spend with no cash under it
    is left to the day's rate; one only partly covered takes the rest at the
    day's rate when it is recorded, else is left whole."""
    rates = fx.stored(log)
    kept: dict[str, dict] = {}
    moved: set[str] = set()
    said: dict[str, str] = {}
    school: dict[str, dict] = {}
    for e in log:
        kind, payload = e.get("event_type"), e.get("payload", {})
        sha = str(payload.get("sha256") or "")
        if kind == LINE_EVENT:
            kept.setdefault(sha, payload)
        elif kind == TRANSFER_EVENT:
            moved.add(sha)
        elif kind == PURPOSE_EVENT and payload.get("cash"):
            said[sha] = str(payload["cash"])
        elif kind == SCHOOL_EVENT:
            school.setdefault(sha, payload)
    pools: dict[tuple[str, str], list[_Cash]] = {}
    for sha, p in sorted(kept.items(), key=lambda kv: (kv[1]["day"], kv[1]["n"], kv[0])):
        line = Line.of(p)
        got = _foreign(said.get(sha, ""))
        if line.kind != ATM or sha in moved or got is None or got[1] <= 0:
            continue
        exact = line.drawn_on
        usable = exact or (date.fromisoformat(line.day) - timedelta(days=NEAR_DAYS)).isoformat()
        pools.setdefault((str(p["person"]), got[0]), []).append(
            _Cash(exact or line.day, usable, got[1], -line.cents, p["account"] == MINISTRY, got[1])
        )
    for pool in pools.values():
        pool.sort(key=lambda c: c.usable)  # stable: two the same day keep the order above
    tags, _landed = _receipt_tags(log)
    behind = held(log)  # riding a line, or one Tim asks about: never cash
    own = splits(log)
    spends = [(day, sha, person, tag) for sha, day, tag, person in tags if sha not in behind]
    spends += [  # a split receipt paid from cash: the person's part spent the cash too
        (day, sha + OWN_PART, person, own[sha])
        for sha, day, _tag, person in tags
        if sha in own and sha not in behind
    ]
    spends += [
        (str(p["day"]), sha, str(p["person"]), f"{p.get('currency') or 'USD'}{p['amount']}")
        for sha, p in school.items()
    ]
    spends += [
        (str(u["day"]), str(u["sha256"]), str(u["person"]), str(u["tag"])) for u in used(log)
    ]
    drawn: dict[str, fx.Drawn] = {}
    late = {str(u["sha256"]) for u in used(log)}  # a count comes at the end of its day
    for day, sha, person, tag in sorted(spends, key=lambda s: (s[0], s[1] in late, s[1])):
        got = _foreign(tag)
        if got is None or (through and day > through):
            continue
        currency, rupees = got
        pool = [c for c in pools.get((person, currency), []) if c.usable <= day and c.left > 0]
        pool.sort(key=lambda c: not c.ministry)  # stable: oldest first within each
        have = sum((c.left for c in pool), Decimal(0))
        rest = max(rupees - have, Decimal(0))
        day_rate = rates.get((currency, day))
        if not pool or (rest and day_rate is None):
            continue
        cost = Decimal(day_rate.cents(rest)) if rest else Decimal(0)
        need, theirs = rupees - rest, Decimal(0)
        for c in pool:
            take = min(need, c.left)
            cost += c.cost(take)
            theirs += 0 if c.ministry else c.cost(take)
            c.left -= take
            need -= take
            if not need:
                break
        cents = int(cost.quantize(Decimal(1), ROUND_HALF_UP))
        per_usd = (rupees * 100 / Decimal(max(cents, 1))).quantize(Decimal("0.0001"))
        own = int(theirs.quantize(Decimal(1), ROUND_HALF_UP)) if pool[0].ministry else 0
        drawn[sha] = fx.Drawn(cents, str(per_usd), pool[0].day, pool[0].ministry, own)
    return drawn, pools


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
    from . import processors

    rates = fx.stored(log)
    to_bank, _paid_out = processors.moves(log)
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
    drawn, _on_hand = cash(log)
    own = splits(log)
    parts = {sha: tag for sha, _day, tag, _person in _receipt_tags(log)[0]}
    out_lines = {
        sha: Line.of(p) for sha, p in kept.items() if Line.of(p).kind == OUT and sha not in moved
    }
    whose = {sha: str(p["person"]) for sha, p in kept.items()}
    riding, doubt = seat(receipts, out_lines, whose, rode(log))
    received = pay_received(kept, purpose, moved)
    behind = {line: receipt for receipt, line in riding.items()}
    out: list[dict] = []
    for sha, p in sorted(kept.items(), key=lambda kv: (kv[1]["day"], kv[1]["n"])):
        if str(p["day"])[:7] != month:
            continue
        line = Line.of(p)
        mine = p["account"] == OWN
        receipt = behind.get(sha, "")
        said = purpose.get(sha, {})
        role = _role(line, mine, sha in moved, receipt in reported, said)
        if sha in received and role == PAID_BACK:
            role = PAY_IN  # their pay from the ministry, arriving (#651)
        if sha in to_bank:  # a payment service's payout landing (c32)
            role = MOVED
        looks = str(p.get("looks", ""))
        category = (
            placed.get(sha)
            or said.get("category")
            or placed.get(receipt, "")
            or reading_cat.get(receipt, "")
            or (canonical(looks, list(categories)) or "" if looks else "")
        )
        if not category and line.bank_fee:
            category = next((c for n in BANK_FEES if (c := canonical(n, list(categories)))), "")
        explained = bool(receipt or said.get("said") or category or line.bank_fee)
        waiting = WAITING if role in SPENDING and not explained else ""
        if role == CASH and not _foreign(str(said.get("cash", ""))):
            waiting = WAITING_CASH
        if sha in doubt and role in SPENDING:
            waiting = WAITING_WHICH
        cents, theirs = _split_cents(abs(line.cents), parts.get(receipt, ""), own.get(receipt, ""))
        if theirs and role == MINISTRY_PAID:  # their part of the ministry's money: owed to it
            out.append(_repay(sha, p, line.day, f"Your part of {line.words}", theirs, BANK))
        out.append(
            {
                "sha256": sha,
                "person": str(p["person"]),
                "account": str(p["account"]),
                "day": line.day,
                "words": line.words,
                "cents": cents if role in SPENDING else abs(line.cents),
                "role": role,
                "receipt": receipt,
                "receipt_file": str(landed.get(receipt, {}).get("filed_to", "")),
                "category": category,
                "said": str(said.get("said", "")),
                "waiting_on": waiting,
                "gave": cash_words(str(said.get("cash", ""))) if role == CASH else "",
                "reported": sha in reported,
                "paid_from": BANK if p["account"] == MINISTRY else PERSON,
                **(
                    {"paid_to": str(said["pays_back"])}
                    if role == PAID_BACK and said.get("pays_back")
                    else {}
                ),
                **(
                    {
                        "paid_to": str(said["pay_of"]),
                        "category": canonical(PAY_KIND, list(categories)) or PAY_KIND,
                    }
                    if role == PAY_OUT
                    else {}
                ),
                **_gift(role, said),
                **({"processor": str(to_bank[sha]["service"])} if sha in to_bank else {}),
            }
        )
    listed = list(categories)
    for sha, p in sorted(school.items(), key=lambda kv: kv[1]["day"]):
        if str(p["day"])[:7] != month:
            continue
        name = school_name(p)
        fields = fx.priced(name, rates, drawn.get(sha))
        d = drawn.get(sha)
        # The ministry's money unless the list says they paid it themselves (w-school-cash).
        paid = d.ministry if d is not None else p.get("paid_by") != OWN
        cents = fields.get("amount_cents") if fields else parse_amount_tag(name)
        out.append(
            {
                "sha256": sha,
                "person": str(p["person"]),
                "account": "",
                "day": str(p["day"]),
                "words": f"{p['student']}, {p['school']}",
                "cents": cents,
                "role": MINISTRY_PAID if paid else OWED,
                "paid_from": CASH_IN_HAND if d is not None and d.ministry else PERSON,
                "school": True,
                "claim_name": name,
                "receipt": "",
                "receipt_file": "",
                "category": placed.get(sha)
                or next((c for n in SCHOOL_CATEGORIES if (c := canonical(n, listed))), ""),
                "said": "",
                "waiting_on": "the day's exchange rate" if cents is None else "",
                "reported": sha in reported,
                **_owed_part(d),
                **{k: v for k, v in fields.items() if k.startswith(("said_", "fx_"))},
            }
        )
    out += _cash_rows(log, month, drawn, rates, listed, placed, reported)
    out += processors.month_rows(log, month, listed)
    return _split_rows(out, line_splits(log), listed)


def _gift(role: str, said: dict) -> dict:
    """Who gave the ministry this money in, as said, and whether it was a
    grant from a donor-advised fund (c32), and whose fund it was given
    toward (c34 PR 3); or who gave their own money in to them, a gift for
    them on that one line (c38)."""
    if role not in (CAME_IN, THEIRS_IN) or not said.get("gift_from"):
        return {}
    return {
        "gift_from": str(said["gift_from"]),
        **({"fund_grant": True} if role == CAME_IN and said.get("fund_grant") else {}),
        **({"for_fund": str(said["for_fund"])} if role == CAME_IN and said.get("for_fund") else {}),
    }


def pay_received(kept: dict[str, dict], purpose: dict[str, dict], moved: set[str]) -> set[str]:
    """The deposits on people's own accounts that are the ministry's pay to
    them arriving (#651): for each person paid, their pay lines out of the
    ministry's account and their own deposits of the same dollars, matched
    as a payback is (``match_transfers``: within a few days, each once, the
    nearest first), even a pay line matched as a payback first (#653). A
    line carries its person's name, the pay line their id.
    Code decides; nobody is asked."""
    from ...tools.viewer import unit_of

    out: set[str] = set()
    paid = {str(purpose[sha]["pay_of"]) for sha in kept if purpose.get(sha, {}).get("pay_of")}
    for who in sorted(paid):
        pay = [
            Line.of(p)
            for sha, p in kept.items()
            if p["account"] == MINISTRY and str(purpose.get(sha, {}).get("pay_of", "")) == who
        ]
        own = {
            Line.of(p): sha
            for sha, p in kept.items()
            if p["account"] == OWN and unit_of(str(p["person"])) == who and sha not in moved
        }
        out |= {own[o] for _m, o in match_transfers(pay, list(own))}
    return out


def _role(line: Line, mine: bool, moved: bool, reported: bool, said: dict) -> str:
    """What one kept line is, from its kind, its account, and what was said."""
    role = (
        SENT
        if line.kind == SEND
        else CASH
        if line.kind == ATM
        else PAID_BACK
        # A line said to be someone's pay is that pay, even when it was
        # matched as a payback first (#653).
        if (moved and not said.get("pay_of")) or (mine and line.kind == IN)
        else CAME_IN
        if line.kind == IN
        else OWED
        if mine
        else MINISTRY_PAID
    )
    if reported:
        return REPORTED  # its receipt is already in a report: never twice the money
    if role == MINISTRY_PAID and said.get("pays_back"):
        return PAID_BACK  # the ministry paying someone back what it owed them (c28)
    if role == MINISTRY_PAID and said.get("pay_of"):
        return PAY_OUT  # the board's pay to a person (c34)
    if role == OWED and said.get("whose") == PAY:
        return FROM_PAY  # paid from their own pay: nobody owes it back (c33)
    if role == CAME_IN and said.get("whose") == OWN:
        return THEIRS_IN  # their own money in: their fund, never income (c31)
    if role in (MINISTRY_PAID, SENT) and said.get("whose") == OWN and not mine:
        # Their own on the ministry's account: owed to it (2026-10-25); a send
        # too, medicine for themselves sent home (2026-10-29).
        return REPAY
    return role


def shares(cents: int, percents: list[int]) -> list[int]:
    """``cents`` in parts by whole percents that add to 100, to the cent:
    each part rounded down, and the cents left over go to the parts that
    lost the most to rounding (largest remainder)."""
    return apportion(cents, percents)


def apportion(cents: int, weights: list[int]) -> list[int]:
    """``cents`` in parts in proportion to ``weights``, to the cent, by
    largest remainder: percents that add to 100, or the amounts a person
    said in the line's own money."""
    total = sum(weights)
    if total <= 0:
        return [0 for _ in weights]
    raw = [cents * w for w in weights]
    out = [r // total for r in raw]
    left = cents - sum(out)
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] % total), i))[:left]:
        out[i] += 1
    return out


@dataclass(frozen=True)
class Take:
    """An amount a person took out of a line in their own words (the owner,
    2026-11-10: "1,300 of that was mine", "5,000 of that was scholarships"),
    in the money the line reads in. The rest stays where it was."""

    tag: str
    category: str = ""
    whose: str = MINISTRY

    @property
    def key(self) -> str:
        return OWN if self.whose == OWN else self.category.casefold()


@dataclass
class LineSplit:
    """What a line is split into: the percents said last (the old way, kept
    readable for the splits already in the books) and the amounts taken out
    since, one per kind, the newest for a kind replacing the one before."""

    parts: list[dict] = field(default_factory=list)
    takes: dict[str, Take] = field(default_factory=dict)


def line_splits(log: list[dict]) -> dict[str, LineSplit]:
    """Each split line, as said (the owner, 2026-10-29; by amount 2026-11-10)."""
    out: dict[str, LineSplit] = {}
    for e in log:
        p = e.get("payload", {})
        if e.get("event_type") != LINE_SPLIT_EVENT or not p.get("sha256"):
            continue
        split = out.setdefault(str(p["sha256"]), LineSplit())
        if p.get("take"):
            take = Take(str(p["take"]), str(p.get("category") or ""), str(p.get("whose") or ""))
            split.takes.pop(take.key, None)  # said again: it moves to the end, replaced
            split.takes[take.key] = take
        else:  # a split by percent replaces everything said before it
            out[str(p["sha256"])] = LineSplit(parts=list(p.get("parts") or []))
    return out


def taken(split: LineSplit | None, but: str = "") -> list[Take]:
    """The amounts taken out of a line, all but the kind ``but`` names."""
    return [t for k, t in (split.takes.items() if split else ()) if k != but]


def _line_money(row: dict) -> tuple[str, int]:
    """The money a line reads in and its whole, in cents of that money: the
    rupees a person said for cash or a school payment, else its dollars."""
    whole, currency = row.get("said_amount"), row.get("said_currency")
    if whole and currency:
        return str(currency), int(Decimal(str(whole)) * 100)
    return "USD", int(row["cents"])


def money(cents: int | None) -> str | None:
    """Exact dollars as a string; a float never carries money."""
    return None if cents is None else f"{Decimal(int(cents)) / 100:.2f}"


def take_cents(take: Take, currency: str) -> int | None:
    """A take's cents in the line's money, or None when it is in another."""
    got = amount_of(take.tag)
    if got is None or got[0] != currency:
        return None
    return int(got[1] * 100)


def _split_rows(rows: list[dict], split: dict[str, LineSplit], listed: list[str]) -> list[dict]:
    """A split line read as its parts, each under its own kind, adding up to
    the line to the cent. Only spending splits: a line that only moved, or is
    owed to the ministry, reads whole. An amount taken out comes off the whole
    first; what is left stays where it was, or divides by the old percents."""
    out = []
    for row in rows:
        said = split.get(row["sha256"])
        if not said or row["role"] not in SPENDING or row["cents"] is None:
            out.append(row)
            continue
        currency, whole = _line_money(row)
        takes = [(t, c) for t in said.takes.values() if (c := take_cents(t, currency)) is not None]
        rest = whole - sum(c for _t, c in takes)
        if not said.parts and not takes or rest < 0:
            out.append(row)  # nothing readable, or more taken than the line: read it whole
            continue
        # Each piece: its weight in the line's money, its kind, its label.
        pieces: list[tuple[int, str, str, Take | None]] = []
        if said.parts and (rest or not takes):
            # Amounts that take the whole line leave the percents nothing to
            # divide: no $0.00 rows (Caleb's 30% pay said again as his own).
            percents = [int(p["percent"]) for p in said.parts]
            for part, w in zip(said.parts, apportion(rest, percents), strict=True):
                pieces.append((w, str(part["category"]), f"{part['percent']}%", None))
        elif rest:
            pieces.append((rest, str(row["category"]), "", None))
        pieces += [(c, t.category, "", t) for t, c in takes]
        weights = [w for w, *_ in pieces]
        if not takes:  # the old way, priced as it always was: by the percents themselves
            weights = [int(p["percent"]) for p in said.parts]
        dollars = apportion(row["cents"], weights)
        owed = apportion(int(row.get("owed_cents") or 0), weights)
        for n, ((w, kind, label, take), cents, theirs) in enumerate(
            zip(pieces, dollars, owed, strict=True), 1
        ):
            piece = {**row, "cents": cents, "category": canonical(kind, listed) or kind}
            piece["part"] = label or _tag_of(currency, w)
            piece["part_no"] = n  # its own line in the report and the ledger
            if row.get("owed_cents"):
                piece["owed_cents"] = theirs
            if currency != "USD":
                piece["said_amount"] = f"{Decimal(w) / 100:.2f}"
            if take is not None and take.whose == OWN:
                if row["role"] == OWED:
                    continue  # their own money on their own account: nobody's owed it
                piece |= {"role": REPAY, "category": "", "owed_cents": 0}
            out.append(piece)
    return out


def _tag_of(currency: str, cents: int) -> str:
    amount = Decimal(cents) / 100
    return f"${amount:,.2f}" if currency == "USD" else f"{currency}{amount:.2f}"


def _owed_part(d: fx.Drawn | None) -> dict:
    """The person's own cash in a spend the ministry's cash paid first: owed
    back to them, and only that part (the owner, 2026-10-16)."""
    return {"owed_cents": d.own_cents} if d is not None and d.ministry and d.own_cents else {}


def _split_cents(cents: int, part: str, theirs: str) -> tuple[int, int]:
    """A line's cents into the ministry's part and the person's, in the
    proportion the split receipt standing behind it says; all the
    ministry's when nothing is split."""
    a, b = amount_of(part), amount_of(theirs)
    if a is None or b is None or a[0] != b[0] or a[1] + b[1] <= 0:
        return cents, 0
    mine = int((Decimal(cents) * a[1] / (a[1] + b[1])).quantize(Decimal(1), ROUND_HALF_UP))
    return mine, cents - mine


def _repay(sha: str, p: dict, day: str, words: str, cents: int, paid_from: str) -> dict:
    """A row of ministry money the person spent on themselves: owed to the
    ministry, never spending, never on the card's total. ``paid_from`` is
    the bank or the cash in hand it came out of (the double-entry books)."""
    return {
        "sha256": sha + OWN_PART,
        "person": str(p["person"]),
        "account": "",
        "day": day,
        "words": words,
        "cents": cents,
        "role": REPAY,
        "receipt": "",
        "receipt_file": "",
        "category": "",
        "said": "",
        "waiting_on": "",
        "gave": "",
        "reported": False,
        "paid_from": paid_from,
    }


def _cash_rows(
    log: list[dict],
    month: str,
    drawn: dict[str, fx.Drawn],
    rates: dict,
    listed: list[str],
    placed: dict[str, str],
    reported: set[str] | frozenset[str],
) -> list[dict]:
    """The month's cash gone with no receipt, and the person's part of a
    split receipt paid from the ministry's cash (t-cash-count,
    t-split-receipt). The ministry's own cash spent on the ministry is the
    ministry paid; the person's own cash spent on it is owed back; the
    ministry's cash spent on the person is owed to the ministry."""
    out = []
    told = _told(log, CASH_USED_EVENT)
    for u in used(log):
        if str(u["day"])[:7] != month:
            continue
        sha, d = str(u["sha256"]), drawn.get(str(u["sha256"]))
        if u.get("whose") == "own":
            if d is not None and d.ministry:  # the ministry's part; their own is theirs
                words = f"Cash for your own use ({u.get('said', '')})"
                row = _repay(sha, u, str(u["day"]), words, d.cents - d.own_cents, CASH_IN_HAND)
                out.append(row | told.get(sha, {}))
            continue
        name = used_name(u)
        fields = fx.priced(name, rates, d)
        cents = fields.get("amount_cents") if fields else None
        category = (
            placed.get(sha)
            or str(u.get("category") or "")
            or next((c for n in CASH_CATEGORIES if (c := canonical(n, listed))), "")
        )
        out.append(
            {
                "sha256": sha,
                "person": str(u["person"]),
                "account": "",
                "day": str(u["day"]),
                "words": f"Cash with no receipt: {u.get('said', '')}",
                "cents": cents,
                "role": MINISTRY_PAID if d is not None and d.ministry else OWED,
                "paid_from": CASH_IN_HAND if d is not None and d.ministry else PERSON,
                "cash_used": True,
                "claim_name": name,
                "receipt": "",
                "receipt_file": "",
                "category": category,
                "said": str(u.get("said", "")),
                "waiting_on": "the day's exchange rate" if cents is None else "",
                "gave": "",
                "reported": sha in reported,
                **_owed_part(d),
                **{k: v for k, v in fields.items() if k.startswith(("said_", "fx_"))},
                **told.get(sha, {}),
            }
        )
    tags = {sha: (day, person) for sha, day, _tag, person in _receipt_tags(log)[0]}
    for sha in splits(log):
        d = drawn.get(sha + OWN_PART)
        day, person = tags.get(sha, ("", ""))
        if d is not None and d.ministry and day[:7] == month:
            cents = d.cents - d.own_cents  # the ministry's cash in their part, owed to it
            out.append(
                _repay(sha, {"person": person}, day, "Your part of a receipt", cents, CASH_IN_HAND)
            )
    return out


def attached(log: list[dict]) -> set[str]:
    """Receipts standing behind a kept line: the line carries them, and they
    are never a line of their own."""
    return set(_seating(log)[0])


def in_doubt(log: list[dict]) -> dict[str, list[str]]:
    """Card lines Tim asks about: each line sha, with the person's receipts
    that could each be the one it carried. Until they say, none of those is
    cash or a line of its own, and the line waits."""
    return _seating(log)[1]


def held(log: list[dict]) -> set[str]:
    """Receipts that are no line of their own: standing behind a line, or
    one of the receipts Tim asks about."""
    behind, doubt = _seating(log)
    return set(behind) | {r for receipts in doubt.values() for r in receipts}


def rode(log: list[dict]) -> dict[str, str]:
    """Which card line each receipt rode, as the person said (the latest)."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"].get("line", ""))
        for e in log
        if e.get("event_type") == RODE_EVENT and e.get("payload", {}).get("sha256")
    }


def _seating(log: list[dict]) -> tuple[dict[str, str], dict[str, list[str]]]:
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
    return seat(receipts, lines, {s: str(p["person"]) for s, p in kept.items()}, rode(log))


def came_in(log: list[dict], start: str, end: str) -> int:
    """What came into the ministry's own account from ``start`` through
    ``end``, in cents: its gross receipts for a 990 (t-990n). Money that
    only moved back (a matched transfer), the person's own account, and
    money in that was the person's own (c31) are never the ministry's.
    Gifts through a payment service count at their gross, there, and the
    bank deposit its payout became never counts again (c32)."""
    from . import processors

    to_bank, _paid_out = processors.moves(log)
    kept: dict[str, dict] = {}
    moved: set[str] = set()
    said: dict[str, dict] = {}
    for e in log:
        payload = e.get("payload", {})
        if e.get("event_type") == LINE_EVENT:
            kept.setdefault(str(payload["sha256"]), payload)
        elif e.get("event_type") == TRANSFER_EVENT:
            moved.add(str(payload.get("sha256")))
        elif e.get("event_type") == PURPOSE_EVENT:
            said[str(payload.get("sha256"))] = payload  # said again, the last word stands
    return processors.gross_in(log, start, end) + sum(
        Line.of(p).cents
        for sha, p in kept.items()
        if p["account"] == MINISTRY
        and sha not in moved
        and sha not in to_bank
        and said.get(sha, {}).get("whose") != OWN
        and Line.of(p).kind == IN
        and start <= str(p["day"]) <= end
    )


def month_end(month: str) -> str:
    """The last day of ``month`` (YYYY-MM)."""
    first = date.fromisoformat(f"{month}-01")
    return (
        (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    ).isoformat()


def repay_words(lines: list[dict]) -> str:
    """The month's ministry money the person spent on themselves, for the
    card: owed to the ministry, never on the total (t-cash-count). The
    reviewer reads it, so it names whose part it is (the dress rehearsal,
    2026-10-15: "Your part of" read as the reviewer's own)."""

    def whose(ln: dict) -> str:
        own = f"{ln['person']}'s own"
        return (
            str(ln["words"])
            .replace("Your part of", f"{own} part of", 1)
            .replace("Cash for your own use", f"{own} use of the ministry's cash", 1)
        )

    parts = [
        f"{ln['day']}: ${ln['cents'] / 100:,.2f} owed to the ministry ({whose(ln)})"
        for ln in lines
        if ln["role"] == REPAY
    ]
    return "; ".join(parts)


def cash_words_at(log: list[dict], person: str, day: str, clock: Clock | None = None) -> str:
    """The person's cash in hand on ``day``, with what it cost and how old
    the oldest of it is, for the reviewer's card (t-advance-clock); past the
    days to return it (120 unless an adopted plan says), and over the most
    the plan lets a person hold, it says so (chunk 3)."""
    clock = clock or Clock()
    _drawn, on_hand = cash(log, through=day)
    age = cash_age(log, day)
    out = []
    held = 0
    for (whose, cur), (rupees, cents) in sorted(on_hand.items()):
        if whose != person:
            continue
        held += cents
        oldest, _left = age.get((whose, cur), (day, Decimal(0)))
        old = days_old(oldest, day)
        past = f", past {clock.back} days unaccounted for" if old > clock.back else ""
        out.append(
            f"{cash_words(f'{cur}{rupees}')} in hand (cost ${cents / 100:,.2f}), "
            f"the oldest drawn {oldest}{past}"
        )
    if clock.cash_up_to is not None and held > clock.cash_up_to * 100:
        out.append(f"that is over the ${clock.cash_up_to:,.2f} the plan lets a person hold")
    return "; ".join(out)


def moved_words(lines: list[dict]) -> str:
    """The month's money that moved but wasn't spending, for the card: what
    was sent abroad, drawn as cash, paid back, and what came in."""
    parts = []
    for ln in lines:
        if ln["role"] in (SENT, CASH, PAID_BACK, CAME_IN, THEIRS_IN, PAY_IN):
            parts.append(f"{ln['day']}: ${ln['cents'] / 100:,.2f} {ln['role']} ({ln['words']})")
    return "; ".join(parts)


__all__ = [
    "ACCOUNTS",
    "ATM",
    "THEIRS_IN",
    "FROM_PAY",
    "MOVED",
    "PROCESSOR",
    "names_a_person",
    "CASH",
    "IN",
    "LINE_EVENT",
    "MINISTRY",
    "OUT",
    "OWN",
    "PAY",
    "SCHOOL_EVENT",
    "SEND",
    "TRANSFER_EVENT",
    "Line",
    "NotAStatement",
    "Receipt",
    "SchoolPayment",
    "attach",
    "cash",
    "cash_words",
    "is_school_list",
    "looks_like",
    "match_transfers",
    "parse",
    "parse_school",
]
