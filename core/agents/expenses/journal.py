"""The books in double entry (board chunk c28, t-books-ledger; the owner,
2026-11-12: a thin accounting layer inside Tim for small ministries).

Tim already keeps every line a ministry's money touched: bank lines,
receipts, school payments, ATM cash and what it bought, splits, what is
owed back to a person and what a person owes the ministry. This module
reads those same rows, as the month report gives them, and writes each as
a balanced journal entry. Code only, no model: the entries are a
projection, never a second record, so the journal can be rebuilt from the
event log at any time and always says what the books say.

The accounts a small ministry needs, and no more:

- ``Assets:Bank:<person>``: the ministry's account whose statements that
  person sends.
- ``Assets:Cash:<person>``: ATM cash in hand, at what it cost in dollars.
- ``Liabilities:People:<person>``: the running account with one person. A
  credit is money the ministry owes them (they paid for the ministry); a
  debit is ministry money they hold or owe (sent to them, paid back to
  them, spent on themselves). An accountable plan in one line. It is also
  the missionary's fund (c31): money in that was their own (their pay from
  a support platform, a gift given for them) credits it, never income, and
  their pay and what they spend on themselves draw from it.
- ``Income:<kind>`` and ``Expenses:<kind>``: the ministry's own categories,
  ``Uncategorized`` when a line has none yet (as the books tool says it).
- ``Assets:Processor:<service>:<person>``: what a payment service the
  ministry takes gifts through holds (c32, processors.py): a gift's net in,
  its payout to the bank out. Never lines on the bank's account, so the
  bank keeps tying to the bank's own balance.
- ``Equity:Opening``: what the bank held before its first kept line.

Every entry's debits equal its credits by construction, and ``check``
proves it for any span. A line still waiting on its amount (a rate not yet
recorded) is no entry yet; it is counted, never guessed.

Pure functions over the month report's rows: no I/O, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import statements as st

BANK, CASH, PEOPLE = "Assets:Bank", "Assets:Cash", "Liabilities:People"
PROCESSOR = "Assets:Processor"  # processors.ROOT
INCOME, EXPENSES, OPENING = "Income", "Expenses", "Equity:Opening"
UNCATEGORIZED = "Uncategorized"

FROM_BANK, FROM_CASH, FROM_PERSON, FROM_PROCESSOR = (
    st.BANK,
    st.CASH_IN_HAND,
    st.PERSON,
    st.PROCESSOR,
)
"""Where a row's money came from (statements.month_lines ``paid_from``)."""


@dataclass(frozen=True)
class Entry:
    """One journal entry: its day, its words, and its legs, each an account
    and signed cents (a debit is positive, a credit negative)."""

    day: str
    words: str
    legs: tuple[tuple[str, int], ...]
    source: str

    def balanced(self) -> bool:
        return sum(c for _a, c in self.legs) == 0


def _cents(dollars: object) -> int | None:
    if dollars is None or dollars == "":
        return None
    return int(Decimal(str(dollars)) * 100)


def _kind(row: dict) -> str:
    return str(row.get("category") or "") or UNCATEGORIZED


def _source_account(paid_from: str, person: str, row: dict | None = None) -> str:
    if paid_from == FROM_PROCESSOR:
        return f"{PROCESSOR}:{(row or {}).get('processor') or 'Unnamed'}:{person}"
    return {FROM_BANK: BANK, FROM_CASH: CASH}.get(paid_from, PEOPLE) + f":{person}"


def _entry(day: str, words: str, source: str, *legs: tuple[str, int]) -> Entry:
    kept = tuple((a, c) for a, c in legs if c)
    return Entry(day, words, kept, source)


def _spend(person: str, row: dict, cents: int, owed: int, paid_from: str) -> Entry:
    """Spending: the kind it was, paid from where the money came from, and
    the person's own part, if any, owed back to them."""
    return _entry(
        str(row.get("date") or ""),
        str(row.get("words") or row.get("for") or ""),
        str(row.get("source") or ""),
        (f"{EXPENSES}:{_kind(row)}", cents),
        (_source_account(paid_from, person, row), -(cents - owed)),
        (f"{PEOPLE}:{person}", -owed),
    )


def _receipt(person: str, line: dict) -> Entry | None:
    """A receipt the person sent: owed back to them, unless the ministry's
    own cash paid it (then only their part of it is)."""
    cents = _cents(line.get("dollars"))
    if cents is None:
        return None
    owed = _cents(line.get("owed_back")) or 0
    if line.get("paid"):
        return _spend(person, line, cents, owed, FROM_CASH)
    return _spend(person, line, cents, cents, FROM_PERSON)


def _bank_row(person: str, row: dict) -> Entry | None:
    """One statement line, school payment or cash row, by what it is."""
    cents = _cents(row.get("dollars"))
    if cents is None:
        return None
    role, paid_from = row.get("role"), str(row.get("paid_from") or FROM_PERSON)
    day, words, source = (
        str(row.get("date") or ""),
        str(row.get("words") or ""),
        str(row.get("source") or ""),
    )
    bank, me = f"{BANK}:{person}", f"{PEOPLE}:{row.get('paid_to') or person}"
    if role in st.SPENDING:
        return _spend(person, row, cents, _cents(row.get("owed_back")) or 0, paid_from)
    if role == st.CAME_IN:  # into the bank, or into a payment service (c32)
        into = _source_account(paid_from, person, row) if paid_from == FROM_PROCESSOR else bank
        return _entry(day, words, source, (into, cents), (f"{INCOME}:{_kind(row)}", -cents))
    if role == st.MOVED:  # the deposit carries the move; the payout is its mirror
        if paid_from != FROM_BANK:
            return None
        service = _source_account(FROM_PROCESSOR, person, row)
        return _entry(day, words, source, (bank, cents), (service, -cents))
    if role == st.THEIRS_IN:  # their own money in: their fund holds it (c31)
        return (
            _entry(day, words, source, (bank, cents), (me, -cents))
            if paid_from == FROM_BANK
            else None
        )
    if role == st.PAY_OUT:  # the board's pay to a person: spending under Pay (c34)
        return (
            _entry(day, words, source, (f"{EXPENSES}:{_kind(row)}", cents), (bank, -cents))
            if paid_from == FROM_BANK
            else None
        )
    if role == st.REPAY:  # ministry money spent on themselves: they owe it
        return _entry(day, words, source, (me, cents), (_source_account(paid_from, person), -cents))
    if paid_from != FROM_BANK:
        return None  # their own account: the mirror of a ministry line, or their own money
    if role == st.CASH:
        return _entry(day, words, source, (f"{CASH}:{person}", cents), (bank, -cents))
    if role in (st.SENT, st.PAID_BACK, st.REPORTED):
        # Money that moved to the person: held by them, paid back to them, or
        # a card line whose receipt they already claimed.
        return _entry(day, words, source, (me, cents), (bank, -cents))
    return None


def month_entries(people: list[dict]) -> tuple[list[Entry], int]:
    """One month report's people as journal entries, and how many of its
    rows are no entry yet (their amount still waits)."""
    out: list[Entry] = []
    waiting = 0
    for p in people:
        person = str(p["person"])
        for line in p.get("lines", []):
            e = _receipt(person, line)
            out.append(e) if e is not None else None
            waiting += e is None
        for row in p.get("bank", []):
            if row.get("dollars") is None:
                waiting += row.get("role") in (*st.SPENDING, st.CAME_IN)
                continue
            e = _bank_row(person, row)
            if e is not None:
                out.append(e)
    return [e for e in out if e.legs], waiting


def opening(person: str, day: str, cents: int) -> Entry:
    """What the ministry's account held before its first kept line."""
    return _entry(
        day,
        "Opening balance",
        "bank.balances",
        (f"{BANK}:{person}", cents),
        (OPENING, -cents),
    )


def totals(entries: list[Entry]) -> dict[str, int]:
    """Each account's balance over the entries, in signed cents."""
    out: dict[str, int] = {}
    for e in entries:
        for account, cents in e.legs:
            out[account] = out.get(account, 0) + cents
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class Check:
    """Whether a span's journal balances: every entry, and the whole."""

    entries: int
    debits: int
    credits: int
    unbalanced: tuple[Entry, ...]

    @property
    def balanced(self) -> bool:
        return not self.unbalanced and self.debits == self.credits


def check(entries: list[Entry]) -> Check:
    debits = sum(c for e in entries for _a, c in e.legs if c > 0)
    credits = -sum(c for e in entries for _a, c in e.legs if c < 0)
    return Check(len(entries), debits, credits, tuple(e for e in entries if not e.balanced()))


def by_kind(balances: dict[str, int], root: str) -> dict[str, int]:
    """The kinds under ``Income`` or ``Expenses``, as positive cents."""
    sign = -1 if root == INCOME else 1
    return {
        a.split(":", 1)[1]: sign * c for a, c in balances.items() if a.startswith(f"{root}:") and c
    }


def owed(balances: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """From the people accounts: what the ministry owes each person, and what
    each person holds or owes the ministry, as positive cents."""
    to, by = {}, {}
    for a, c in balances.items():
        if a.startswith(f"{PEOPLE}:") and c:
            (to if c < 0 else by)[a.split(":", 2)[2]] = abs(c)
    return to, by
