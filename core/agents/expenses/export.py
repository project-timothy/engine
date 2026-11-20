"""The books a CPA can open without Tim (board chunk c30, t-cpa-export; the
owner, 2026-11-12: nothing locked inside Tim).

A year's double-entry journal (journal.py) written as plain text in the
Beancount format, a trial balance as CSV, and the numbers the year's filings
start from. Beancount is only the text format here: nothing imports it, and
anyone with a text editor, a spreadsheet, or Beancount itself can check the
books.

- Account names become Beancount's: each part capitalized, letters, digits
  and dashes only ("Children's Home" -> "Childrens-Home"); the file's header
  keeps the map back to the ministry's own words.
- A year starts from what was brought forward: every asset and liability as
  of the day before, with the net assets they add up to, so the year's file
  stands alone and balances.
- ``read_back`` reads the file this module writes, checks every transaction
  balances, and returns each account's total, so the export round-trips.

Pure functions: no I/O, no clock.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ...tax.forms import needs_1099_nec
from . import journal as j

BROUGHT_FORWARD = "Equity:Opening"
FORM_990_N_LIMIT = 5_000_000
"""Gross receipts normally at or under $50,000: the year's return is the 990-N."""


def account_name(account: str) -> str:
    """The ministry's account as Beancount spells one."""
    parts = []
    for part in account.split(":"):
        word = re.sub(r"[^A-Za-z0-9]+", "-", part.replace("'", "")).strip("-") or "Unnamed"
        parts.append(word[0].upper() + word[1:])
    return ":".join(parts)


def _amount(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "/").replace('"', "'").replace("\n", " ") + '"'


def brought_forward(before: list[j.Entry], day: str) -> j.Entry | None:
    """Every asset and liability as the year opens, against net assets."""
    totals = j.totals(before)
    legs = [(a, c) for a, c in totals.items() if c and a.startswith(("Assets", "Liabilities"))]
    if not legs:
        return None
    net = sum(c for _a, c in legs)
    return j.Entry(day, "Brought forward", (*legs, (BROUGHT_FORWARD, -net)), "brought-forward")


def beancount(entries: list[j.Entry], *, title: str, notes: list[str] = ()) -> str:
    """The entries as a Beancount-format file, accounts opened on first use."""
    first: dict[str, str] = {}
    for e in sorted(entries, key=lambda e: e.day):
        for a, _c in e.legs:
            first.setdefault(a, e.day)
    lines = [
        f"; {title}",
        "; Written by Tim from the ministry's own books. Plain text in the Beancount format.",
        *(f"; {n}" for n in notes),
        "; Accounts, as the ministry names them:",
        *(f";   {account_name(a)} = {a}" for a in sorted(first) if account_name(a) != a),
        "",
        f'option "title" {_quoted(title)}',
        'option "operating_currency" "USD"',
        "",
    ]
    lines += [
        f"{first[a]} open {account_name(a)} USD" for a in sorted(first, key=lambda a: (first[a], a))
    ]
    for e in sorted(entries, key=lambda e: (e.day, e.source)):
        lines += ["", f"{e.day} * {_quoted(e.words)}", f"  source: {_quoted(e.source)}"]
        lines += [f"  {account_name(a):<50} {_amount(c):>12} USD" for a, c in e.legs]
    return "\n".join(lines) + "\n"


_TXN = re.compile(r"^\d{4}-\d{2}-\d{2} \* ")
_POSTING = re.compile(r"^  ([A-Z][A-Za-z0-9:-]*)\s+(-?\d+\.\d{2}) USD$")


@dataclass(frozen=True)
class ReadBack:
    totals: dict[str, int]
    transactions: int
    unbalanced: int


def read_back(text: str) -> ReadBack:
    """The file this module writes, read again: each account's total, how
    many transactions, and how many don't balance."""
    totals: dict[str, int] = {}
    count = unbalanced = 0
    current: int | None = None
    for line in [*text.splitlines(), ""]:
        if _TXN.match(line):
            if current not in (None, 0):
                unbalanced += 1
            count, current = count + 1, 0
            continue
        found = _POSTING.match(line)
        if found and current is not None:
            cents = int(Decimal(found[2]) * 100)
            totals[found[1]] = totals.get(found[1], 0) + cents
            current += cents
        elif not line.strip() and current is not None:
            unbalanced += current != 0
            current = None
    return ReadBack(dict(sorted(totals.items())), count, unbalanced)


def trial_balance(totals: dict[str, int]) -> str:
    """Each account's debit or credit as CSV, with the totals row."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Account", "Ministry's name", "Debit", "Credit"])
    debits = credits = 0
    for a, c in sorted(totals.items()):
        if not c:
            continue
        debits += max(c, 0)
        credits += max(-c, 0)
        w.writerow([account_name(a), a, _amount(c) if c > 0 else "", _amount(-c) if c < 0 else ""])
    w.writerow(["Total", "", _amount(debits), _amount(credits)])
    return out.getvalue()


def filings(
    year_totals: dict[str, int],
    at_end: dict[str, int],
    pay_kind: str,
    pay: dict[str, int] | None = None,
    year: str = "",
) -> dict:
    """The numbers the year's returns start from: gross receipts and which
    990 they point to; what was spent under the ministry's pay kind; the
    ministry money each person still holds or owes at year end (a return
    may count it as pay, by a year-end entry the ministry decides); and,
    where the ministry paid pay (``church_pay.by_person``, in cents), each
    person's pay and whether it needs a 1099-NEC (c34 PR 3)."""
    receipts = sum(j.by_kind(year_totals, j.INCOME).values())
    spent = sum(j.by_kind(year_totals, j.EXPENSES).values())
    _to, held = j.owed(at_end)
    net = sum(c for a, c in at_end.items() if a.startswith(("Assets", "Liabilities")))
    return {
        "gross_receipts": _amount(receipts),
        "990": "990-N (gross receipts normally $50,000 or less)"
        if receipts <= FORM_990_N_LIMIT
        else "990-EZ or 990 (gross receipts over $50,000)",
        "total_expenses": _amount(spent),
        "net_assets_at_year_end": _amount(net),
        "pay_kind": pay_kind,
        "spent_under_pay": _amount(j.by_kind(year_totals, j.EXPENSES).get(pay_kind, 0)),
        "held_or_owed_by_people": {p: _amount(c) for p, c in sorted(held.items())},
        **(
            {
                "pay_by_person": {
                    p: {"pay": _amount(c), "needs_1099_nec": needs_1099_nec(c, year)}
                    for p, c in sorted(pay.items())
                }
            }
            if pay
            else {}
        ),
    }


def pay_words(numbers: dict, name_of: Callable[[str], str]) -> str:
    """What ``filings`` says of the ministry's pay, one sentence a person
    (c34 PR 3); "" where it paid none."""
    out = ""
    for who, row in numbers.get("pay_by_person", {}).items():
        out += f" {name_of(who)} was paid ${Decimal(row['pay']):,.2f} as pay"
        need = row["needs_1099_nec"]
        out += (
            ", so they need a 1099-NEC."
            if need
            else "."
            if need is False
            else ", and Tim holds no 1099 line for that year to check it against."
        )
    return out
