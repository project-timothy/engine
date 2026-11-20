"""One bill paid with two or more checks: reading the owner's reference.

Issue #688 (2026-11-18). A bank's bill pay can cap a single check (Fifth
Third's is $10,000), so a larger bill goes out as several checks against ONE
ledger row and ONE accounting-system bill. The owner records the split where
he records any instrument, in the row's check reference, one check and its
amount at a time:

    "Checks 9170 + 9171 (bill pay: 9170 $10,000.00 on 11/24, 9171 $3,253.00 on 11/25)"

Code reads it; nothing is guessed. A reference that names two or more checks
is a split. It is usable only when every named check carries exactly one
amount and the amounts sum to the row to the cent; otherwise ``problem`` says
what is missing and how to write it, and no caller acts on a partial reading.
A date after an amount ("on 11/24") is that check's send date; without one
the row's own payment date stands.

Shared by ``qbo_push_payments`` (one partial BillPayment per check) and
``reconcile`` (a statement line pays one piece; the row settles when every
piece has cleared). Imports nothing from either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# Four digits or more is a check number (reconcile.MIN_CHECK_DIGITS, same rule).
_CHECK = re.compile(r"(?<!\d)(\d{4,})(?!\d)")
_AMOUNT = re.compile(r"\$\s*[\d,]+(?:\.\d{1,2})?")
_PIECE = re.compile(
    r"(?<![\d$,.])(\d{4,})(?!\d)\s*[:=]?\s*\$\s*([\d,]+(?:\.\d{1,2})?)"
    r"(?:\s+on\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?)?"
)


@dataclass(frozen=True)
class Piece:
    check: str
    amount_cents: int
    send_date: str  # ISO, or "" when the reference gives none


@dataclass(frozen=True)
class Split:
    checks: tuple[str, ...]  # every check the reference names, in order
    pieces: tuple[Piece, ...]  # complete only when ``ok``
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem

    def piece(self, check: str) -> Piece | None:
        return next((p for p in self.pieces if p.check == check), None)


def named_checks(ref: str) -> tuple[str, ...]:
    """Every check number in the reference, amounts removed first so
    "$12000.00" never reads as a check, each number once, in order."""
    seen: list[str] = []
    for token in _CHECK.findall(_AMOUNT.sub(" ", str(ref or ""))):
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def _cents(raw: str) -> int | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    cents = value * 100
    return int(cents) if cents == cents.to_integral_value() else None


def _send_date(month: str, day: str, year: str, base: date | None) -> str:
    if not month:
        return ""
    try:
        if year:
            y = int(year) + (2000 if len(year) == 2 else 0)
            return date(y, int(month), int(day)).isoformat()
        if base is None:
            return ""
        guess = date(base.year, int(month), int(day))
    except ValueError:
        return ""
    if guess < base - timedelta(days=180):
        guess = guess.replace(year=base.year + 1)
    elif guess > base + timedelta(days=180):
        guess = guess.replace(year=base.year - 1)
    return guess.isoformat()


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def read_split(ref: str, *, total_cents: int, base_date: str) -> Split | None:
    """The split a reference describes, or None when it names one check or
    none. ``base_date`` (the row's payment date, ISO) anchors a send date
    written without a year."""
    checks = named_checks(ref)
    if len(checks) < 2:
        return None
    try:
        base = date.fromisoformat(str(base_date)[:10]) if base_date else None
    except ValueError:
        base = None
    names = " and ".join(checks)
    how = " + ".join(f"{c} $<amount>" for c in checks)
    pieces: list[Piece] = []
    for check, amount, month, day, year in _PIECE.findall(str(ref or "")):
        cents = _cents(amount)
        if cents is None or cents <= 0:
            return Split(checks, (), f"the amount after check {check} is not a dollar amount")
        if any(p.check == check for p in pieces):
            return Split(checks, (), f"the reference gives check {check} two amounts")
        pieces.append(Piece(check, cents, _send_date(month, day, year, base)))
    given = {p.check for p in pieces}
    missing = [c for c in checks if c not in given]
    if missing:
        return Split(
            checks,
            (),
            f"the reference names checks {names} but not each check's amount "
            f"(missing {', '.join(missing)}); write it as '{how}'",
        )
    total = sum(p.amount_cents for p in pieces)
    if total != int(total_cents):
        return Split(
            checks,
            (),
            f"checks {names} add to {_dollars(total)} but the bill is {_dollars(int(total_cents))}",
        )
    return Split(checks, tuple(pieces))
