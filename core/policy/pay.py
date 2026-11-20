"""A missionary's pay resolution, drafted by Tim (c34, the sender model, the
owner's go of 2026-11-14).

The board sets a missionary's pay once a year: a yearly amount, paid in
equal months on a set day, from a month through that December. Tim drafts
the resolution's words (``pay-resolution.txt``, the template beside this
file, with the ministry's and the person's names in it) and its numbers as
one plan rule, which ``policy propose`` raises as one card. The card covers
the person it pays, so they never adopt it; the person's family abstains
(``family`` in authority.toml); a director with no family tie says yes.

A step-down is a new resolution from a later month of the same year. Each
resolution is its own pair of files, named for the person and the month it
starts, so an older one stays as it was adopted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from string import Template

from . import money_words, monthly_cents

TEMPLATE = Path(__file__).resolve().parent / "pay-resolution.txt"


@dataclass(frozen=True)
class Written:
    file: str  # pay-<person>-<YYYY-MM>.toml
    copy: str  # pay-<person>-<YYYY-MM>.txt
    rules: str
    words: str


def ordinal(day: int) -> str:
    """1st, 2nd, 3rd, 4th ... 21st, 22nd, 23rd, 28th."""
    tail = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{tail}"


def _month_words(month: str) -> str:
    return date(int(month[:4]), int(month[5:7]), 1).strftime("%B %Y")


def write(
    ministry: str,
    *,
    person: str,
    name: str,
    yearly: Decimal,
    day: int,
    start: str,
    adopted_by: str,
) -> Written:
    """The resolution's words and its one rule: ``person`` (their id) known
    as ``name``, paid ``yearly`` on ``day`` from ``start`` (YYYY-MM) through
    that December, adopted by the role ``adopted_by``."""
    yearly = yearly.quantize(Decimal("0.01"))
    cents = int(yearly * 100)
    monthly = money_words(Decimal(monthly_cents(cents, 1)) / 100)
    first, last = _month_words(start), f"December {start[:4]}"
    words = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        ministry=ministry,
        name=name,
        yearly=money_words(yearly),
        monthly=monthly,
        day=ordinal(day),
        first=first,
        last=last,
        adopted_by=adopted_by,
    )
    stem = f"pay-{person}-{start}"
    sha = hashlib.sha256(words.encode("utf-8")).hexdigest()
    says = (
        f"{name} is paid {money_words(yearly)} a year, {monthly} on the {ordinal(day)} "
        f"of each month, {first} through {last}."
    )
    quote = f"pays {name}, whom the ministry sends and commissions for its work, " + (
        f"{money_words(yearly)} a year"
    )
    rules = (
        "# A missionary's pay, drafted by Tim (core/policy/pay.py). It goes in force on\n"
        "# one yes from a director with no family tie to the person it pays. A changed\n"
        "# amount is a new resolution from the month it starts.\n"
        'layer = "tenant"\n'
        f"document = {json.dumps(f'{ministry} pay for {name} from {first}')}\n"
        "plan = true\n"
        f"adopted_by = {json.dumps(adopted_by)}\n\n"
        f"[copy]\nfile = {json.dumps(stem + '.txt')}\nsha = {json.dumps(sha)}\n\n"
        "[[rule]]\n"
        f"id = {json.dumps(stem)}\n"
        f"who = {json.dumps([person])}\n"
        f'pay_year = "{yearly:.2f}"\n'
        f"pay_day = {day}\n"
        f"pay_from = {json.dumps(start)}\n"
        f"says = {json.dumps(says)}\n"
        f'source = {{ section = "Resolution", quote = {json.dumps(quote)} }}\n'
    )
    return Written(file=stem + ".toml", copy=stem + ".txt", rules=rules, words=words)


def place(folder: Path, written: Written) -> None:
    """Into the ministry's rules folder, beside the rules it already has."""
    folder.mkdir(exist_ok=True)
    (folder / written.copy).write_text(written.words, encoding="utf-8")
    (folder / written.file).write_text(written.rules, encoding="utf-8")


__all__ = ["Written", "monthly_cents", "ordinal", "place", "write"]
