"""A bank statement's photo or PDF, read into lines that add up (Standalone
Tim, card R2).

Until now a statement reached the books only as the CSV file a bank's
website exports, which many banks hide and many phones can't open. Where
the ministry's file switches it on, a statement sent as a photo or a PDF
through a catch-up link is read by a model. A model's reading of a
statement is a list of dollar amounts, and no model output becomes a number
in the books (engine invariant 2), so the reading is only as good as the
check behind it, which is code, here:

- **It adds up, to the cent.** The opening balance the statement prints,
  plus every line, is the closing balance it prints. When it prints its
  totals in and out, the lines make those too.
- **Every line is in the statement's own period**, and the period has come.
- **Every amount is whole cents**, never zero, in dollars (the books' bank
  lines are dollars, as a CSV's are).

If any of that fails, nothing is proposed and she is asked for the bank's
CSV export. When it holds, the page says how many lines, the period, and the
two balances, and her one tap keeps them. From there they are exactly the
lines a CSV makes (:class:`~core.agents.expenses.statements.Line`, numbered
the way ``statements.parse`` numbers them, the closing balance on the last
day's line as a Balance column gives it), held the same way, with the same
later choices: whose account it is, and which lines are hers.

What the check cannot catch is a reading wrong in two places that cancel
out. Her tap, the statement's balance against the books
(``core/agents/expenses/balances.py``), and her reviewer stand behind it.

- **Off unless the file names it.** Job type ``statement_read``
  (``core.llm.policy.NAMED_ONLY``), held by its own eval set
  (``core/llm/eval_sets/statement_read``, made-up statements, one that does
  not add up).

Boundary classification (docs/boundary-rules.md): the model reads a picture
into lines; code proves the lines are the statement's own arithmetic; her
tap keeps them; the expenses jobs write them, as for a CSV.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..agents.expenses.statements import Line
from ..llm.gateway import DecimalString
from . import doc_read

JOB = "statement_read"
"""The job type a ministry's ``[llm.jobs]`` names to switch the reader on."""

MAX_LINES = 400
"""More lines than a month's statement holds; a reply past it is refused."""

MAX_DAYS = 366
"""The longest period a statement covers that the reader takes."""

MAX_CENTS = 100_000_000_000  # a billion dollars: past any ministry's account

SYSTEM = """\
Reply with ONLY a JSON object (no prose, no code fences) with keys:
  doc_type: "bank_statement" if this is a bank account's statement listing its
    transactions for a period, else "other"
  bank: the bank's name as printed, or null
  currency: the ISO 4217 code of the account's money (USD for dollars), or null
  period_start: the first day the statement covers, ISO YYYY-MM-DD, or null
  period_end: the last day the statement covers, ISO YYYY-MM-DD, or null
  opening_balance: the balance printed for the start of the period, as a
    decimal string like "1234.56" (negative if overdrawn), or null
  closing_balance: the balance printed for the end of the period, the same
    way, or null
  total_in: the total of deposits and credits, when the statement prints one,
    as a decimal string; else null
  total_out: the total of withdrawals, checks, and fees, when the statement
    prints one, as a positive decimal string; else null
  lines: every transaction, in the order printed, each an object with
    day: its date, ISO YYYY-MM-DD (the statement's year when only the month
      and day are printed)
    words: its description as printed
    amount: a decimal string, negative for money out (withdrawals, checks,
      card purchases, fees), positive for money in
Read every page. List each transaction once. Leave out running balances,
subtotals, and summary lines. Write amounts as plain decimal strings with no
currency sign and no thousands commas. Do not guess an amount that is not
printed.
"""
"""The reader's brief; the eval set carries the same words (a test pins it)."""

USER = (
    "Read the attached bank statement. It is a photo or a scan with no text layer, so read "
    "the picture itself."
)

WORDS_FILE = Path(__file__).with_name("statement_read.toml")

_CENTS = re.compile(r"[-+]?\d+(\.\d{1,2})?")


class StatementLineReply(BaseModel):
    """One transaction as the model reads it."""

    day: str = Field(max_length=40)
    words: str = Field(default="", max_length=400)
    amount: DecimalString


class StatementReply(BaseModel):
    """What the model says the statement prints."""

    doc_type: str = Field(default="other", max_length=40)
    bank: str | None = Field(default=None, max_length=200)
    currency: str | None = Field(default=None, max_length=8)
    period_start: str | None = Field(default=None, max_length=40)
    period_end: str | None = Field(default=None, max_length=40)
    opening_balance: DecimalString | None = None
    closing_balance: DecimalString | None = None
    total_in: DecimalString | None = None
    total_out: DecimalString | None = None
    lines: list[StatementLineReply] = Field(default_factory=list, max_length=MAX_LINES + 1)


@dataclass(frozen=True)
class Proven:
    """A statement whose lines code proved are its own arithmetic: the
    lines, oldest first, exactly as a CSV would have made them, the period,
    and the two balances in cents."""

    rows: tuple[Line, ...]
    start: date
    end: date
    opening: int
    closing: int
    bank: str = ""


NOT_A_STATEMENT = "not a bank statement"
""":func:`check`'s reason for a reply that says the file is something else:
the upload goes on the way it always has (a receipt)."""


def on(settings: Any) -> bool:
    """Whether the ministry's file switches the reader on."""
    return doc_read.on(settings, JOB)


def read(
    data: bytes,
    suffix: str,
    *,
    settings: Any,
    ledger_root: str | Path,
    tenant: str,
    person: str,
    today: date,
    adapter: Any = None,
) -> StatementReply | None:
    """The model's reading of the file, unchecked, or None when there is
    none (the reader off, a failure, a refusal, a timeout, the cap). Only
    :func:`check` says what it is worth."""
    return doc_read.ask(
        data,
        suffix,
        job=JOB,
        system=SYSTEM,
        user=USER,
        output_model=StatementReply,
        settings=settings,
        ledger_root=ledger_root,
        tenant=tenant,
        person=person,
        today=today,
        adapter=adapter,
    )


def cents(said: str | None) -> int | None:
    """A decimal string as whole cents, or None: no more than two places
    after the point (a fraction of a cent is a misreading, never rounded)."""
    text = str(said or "").strip()
    if not _CENTS.fullmatch(text):
        return None
    value = Decimal(text) * 100
    if value != value.to_integral_value() or abs(value) > MAX_CENTS:
        return None
    return int(value)


def _day(said: str | None) -> date | None:
    try:
        return date.fromisoformat(str(said or "").strip())
    except ValueError:
        return None


def _words(said: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in said).split())[:200]


def check(reply: StatementReply, today: date) -> tuple[Proven | None, str]:
    """``(proven, "")`` when the reading is the statement's own arithmetic,
    else ``(None, why)``. The whole money rule of the reader is here."""
    if reply.doc_type != "bank_statement":
        return None, NOT_A_STATEMENT
    if str(reply.currency or "").strip().upper() != "USD":
        return None, "not in dollars"
    start, end = _day(reply.period_start), _day(reply.period_end)
    if start is None or end is None or start > end:
        return None, "no period"
    if end > today or (end - start).days > MAX_DAYS:
        return None, "a period that can't be a statement's"
    opening, closing = cents(reply.opening_balance), cents(reply.closing_balance)
    if opening is None or closing is None:
        return None, "no opening or closing balance"
    if not 0 < len(reply.lines) <= MAX_LINES:
        return None, "no lines" if not reply.lines else "too many lines"
    seen: dict[tuple[str, int], int] = {}
    rows: list[Line] = []
    for item in reply.lines:
        day, amount, words = _day(item.day), cents(item.amount), _words(item.words)
        if day is None or not start <= day <= end:
            return None, "a line outside the statement's period"
        if not amount:
            return None, "a line with no amount in whole cents"
        if not words:
            return None, "a line with no description"
        key = (day.isoformat(), amount)
        n = seen.get(key, 0)
        seen[key] = n + 1
        rows.append(Line(day.isoformat(), words, amount, n))
    if opening + sum(r.cents for r in rows) != closing:
        return None, "the lines don't add up to the balances"
    if reply.total_in is not None and cents(reply.total_in) != sum(
        r.cents for r in rows if r.cents > 0
    ):
        return None, "the lines in don't make the total in"
    if reply.total_out is not None:
        out = cents(reply.total_out)
        if out is None or abs(out) != -sum(r.cents for r in rows if r.cents < 0):
            return None, "the lines out don't make the total out"
    rows.sort(key=lambda r: (r.day, r.n))
    rows[-1] = replace(rows[-1], day_end=closing)  # the last day ends at the closing balance
    bank = _words(str(reply.bank or ""))[:60]
    return Proven(tuple(rows), start, end, opening, closing, bank), ""


def dollars(amount: int) -> str:
    """Cents the way a statement prints them: "$1,234.56", "-$12.00"."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount) / 100:,.2f}"


def day_words(day: date) -> str:
    """ "October 1, 2026"."""
    return f"{day:%B} {day.day}, {day.year}"


@cache
def words() -> dict[str, str]:
    """The page's lines for a statement (``statement_read.toml``)."""
    return dict(tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))["words"])


def proposal(proven: Proven) -> str:
    """What the page says of a statement that adds up, for her tap."""
    n = len(proven.rows)
    return (
        words()["reads"]
        .replace("{lines}", f"{n} line" if n == 1 else f"{n} lines")
        .replace("{start}", day_words(proven.start))
        .replace("{end}", day_words(proven.end))
        .replace("{opening}", dollars(proven.opening))
        .replace("{closing}", dollars(proven.closing))
    )


__all__ = [
    "JOB",
    "MAX_LINES",
    "NOT_A_STATEMENT",
    "SYSTEM",
    "USER",
    "Proven",
    "StatementLineReply",
    "StatementReply",
    "cents",
    "check",
    "dollars",
    "on",
    "proposal",
    "read",
    "words",
]
