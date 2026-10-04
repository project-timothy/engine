"""The bank-feed sweep's note, read back: which rows it parked for the owner.

Phase 7 row 7.4 (issue #213), the third card source. The weekly sweep of the
accounting system's bank feed clicks the matches its policy allows and parks
everything else in a dated note, one bullet per feed line under a heading
that names the owner ("Needs <name>"). That note was written for a person to
read. This module reads it back so the engine can ask the question in the
approval queue instead, where every other question is answered.

Three rules shape the parse, and each is defensive on purpose.

**The section is found structurally, never by the owner's name.** A tenant
names its own owner and core never spells one (invariant 5), so the parked
section is "a heading whose text starts with `needs`". Every other section
of the note (the tie-out, the clicks, the rule misses, what was not touched)
is deliberately out of scope: those are statements, not questions.

**The identity of a parked row is the bank's facts, not the note's prose.**
The note is written by a model session, so the same feed line comes out
worded differently from one week to the next; keying a card on the sentence
would ask the same question again every Thursday. The key is the
transaction date, the amount in cents, the direction, and the bank text (the
run of capitals the bank itself puts on the line). Those four survive a
rewrite because none of them is the writer's choice.

**A bullet without money is not a question.** "None." and "nothing sat in
the queue" are how a quiet week is written; they park nothing.

Pure functions, no I/O: ``jobs.py`` supplies the text and acts on the rows.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# The parked section: any heading whose text begins with "needs". The sweep
# note spells the owner's name there, which core must never carry.
_PARKED_HEADING = re.compile(r"^(#{1,6})\s*needs\b", re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})\s+\S")
# A top-level bullet. Continuation lines are indented; nested bullets belong
# to the row above them, which is why only column zero starts a new row.
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
# ISO first, then US month/day with an optional year: the first date token in
# the bullet is the transaction date, which is how the note is written.
_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b|\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_NOTE_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_CHECK = re.compile(r"\bcheck\s*(?:no\.?|number|#)?\s*#?\s*(\d{3,7})\b", re.IGNORECASE)
# The bank's own text on the line, which banks print in capitals.
_UPPER_RUN = re.compile(r"[A-Z][A-Z0-9&./'-]{2,}(?:\s+[A-Z0-9&./'-]{2,})*")
_QUOTED = re.compile(r"[\"“]([^\"”]{2,60})[\"”]")
# Words that are capitalized for emphasis or by convention and say nothing
# about which line this is.
_NOT_BANK_TEXT = frozenset(
    {
        "AND",
        "OR",
        "OFF",
        "OUT",
        "IN",
        "THE",
        "NOT",
        "NEW",
        "NO",
        "YES",
        "ONE",
        "TWO",
        "ALL",
        "NEEDS",
        "REVIEW",
        "TODO",
        "NOTE",
        "PAIR",
        "MATCH",
        "ADD",
        "POST",
        "UNDO",
        "STILL",
        "WAITING",
        "FROM",
        "LAST",
        "WEEK",
    }
)
_OUT_MARKERS = ("spent", "money out", "withdraw", "debit", "paid out", "payment out")
_IN_MARKERS = ("received", "money in", "deposit", "credit")
_EMPTY_BULLET = ("none", "nothing", "n/a", "no rows", "no parked")


@dataclass(frozen=True)
class ParkedRow:
    """One feed line the sweep left for the owner."""

    line_id: str
    date: str
    amount_cents: int
    direction: str  # "out", "in", or "" when the note did not say
    bank_text: str
    check_ref: str
    text: str  # the bullet as written, for the card's reason line

    @property
    def amount(self) -> str:
        return f"${self.amount_cents / 100:,.2f}"


def feed_line_id(date: str, amount_cents: int, direction: str, bank_text: str) -> str:
    """The stable identity of a feed line: the bank's facts, hashed.

    Sixteen hex characters, the same width every other content key in this
    engine uses. Two genuinely different lines collide only if they share a
    date, an amount to the cent, a direction, AND the bank's text, which
    would be indistinguishable in the note as well.
    """
    material = "|".join((date, str(amount_cents), direction, bank_text.upper()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def note_date(text: str, *, filename: str = "") -> str:
    """The note's own date, from its filename or its first ISO date."""
    for candidate in (filename, text[:400]):
        found = _NOTE_DATE.search(candidate or "")
        if found:
            return found.group(0)
    return ""


def _parked_lines(text: str) -> list[str]:
    """Every line inside the parked section, in order."""
    out: list[str] = []
    depth = 0
    inside = False
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            parked = _PARKED_HEADING.match(line)
            level = len(heading.group(1))
            if parked:
                inside, depth = True, level
                continue
            if inside and level <= depth:
                inside = False
            continue
        if inside:
            out.append(line)
    return out


def _bullets(lines: list[str]) -> list[str]:
    """Top-level bullets, each joined with its continuation lines."""
    bullets: list[str] = []
    current: list[str] | None = None
    for line in lines:
        match = _BULLET.match(line)
        if match:
            if current is not None:
                bullets.append(" ".join(current))
            current = [match.group(1).strip()]
        elif current is not None:
            if line.strip():
                current.append(line.strip())
            else:
                bullets.append(" ".join(current))
                current = None
    if current is not None:
        bullets.append(" ".join(current))
    return [re.sub(r"\s+", " ", b).strip() for b in bullets if b.strip()]


def _amount_cents(text: str) -> int:
    found = _AMOUNT.search(text)
    if not found:
        return 0
    raw = found.group(1).replace(",", "")
    whole, _, frac = raw.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2] or 0)


def _date(text: str, default_year: str) -> str:
    found = _DATE.search(text)
    if not found:
        return ""
    if found.group(1):
        return f"{found.group(1)}-{int(found.group(2)):02d}-{int(found.group(3)):02d}"
    month, day, year = found.group(4), found.group(5), found.group(6)
    if year and len(year) == 2:
        year = f"20{year}"
    if not year:
        year = default_year
    if not year:
        return ""
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _direction(text: str) -> str:
    low = text.lower()
    out = min((low.find(m) for m in _OUT_MARKERS if m in low), default=-1)
    inn = min((low.find(m) for m in _IN_MARKERS if m in low), default=-1)
    if out < 0 and inn < 0:
        return ""
    if inn < 0 or (out >= 0 and out < inn):
        return "out"
    return "in"


def _bank_text(text: str) -> str:
    """The bank's own words: the longest run of capitals, else a quoted span.

    Emphasis words are trimmed from the ENDS of a run and never from inside
    it: a line that opens "STILL WAITING from last week" must not become bank
    text, while a card issuer whose name contains "ONE" must survive whole.
    """
    best = ""
    for run in _UPPER_RUN.finditer(text):
        words = run.group(0).strip().split()
        while words and words[0] in _NOT_BANK_TEXT:
            words.pop(0)
        while words and words[-1] in _NOT_BANK_TEXT:
            words.pop()
        candidate = " ".join(words)
        if len(candidate) > len(best):
            best = candidate
    if best:
        return best
    quoted = _QUOTED.search(text)
    return quoted.group(1).strip().upper() if quoted else ""


def _check_ref(text: str) -> str:
    found = _CHECK.search(text)
    return found.group(1) if found else ""


def parse_note(text: str, *, note_date_iso: str = "", filename: str = "") -> list[ParkedRow]:
    """The rows a sweep note parked, in the order the note lists them.

    ``note_date_iso`` (or the note's own date, read from ``filename`` or the
    text) supplies the year for a bullet written as month/day.
    """
    year = (note_date_iso or note_date(text, filename=filename))[:4]
    rows: list[ParkedRow] = []
    seen: set[str] = set()
    for bullet in _bullets(_parked_lines(text)):
        low = bullet.lower().lstrip("*_ ")
        if any(low.startswith(marker) for marker in _EMPTY_BULLET):
            continue
        cents = _amount_cents(bullet)
        if cents <= 0:
            continue
        date = _date(bullet, year)
        direction = _direction(bullet)
        bank_text = _bank_text(bullet)
        line_id = feed_line_id(date, cents, direction, bank_text)
        if line_id in seen:
            continue
        seen.add(line_id)
        rows.append(
            ParkedRow(
                line_id=line_id,
                date=date,
                amount_cents=cents,
                direction=direction,
                bank_text=bank_text,
                check_ref=_check_ref(bullet),
                text=bullet,
            )
        )
    return rows
