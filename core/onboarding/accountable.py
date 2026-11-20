"""The ministry's accountable plan, written at setup (chunk 3, the owner's go
of 2026-10-16).

A small church or a solo ministry starts with a plan: the words a board
adopts (``accountable-plan.txt``, the template beside this file, with no
names in it but the ministry's), and its numbers as rules
(``accountable-plan.toml``) that ``policy propose`` raises as ONE card. The
plan covers everyone who files expenses; whoever adopts it is someone it
does not cover (the board, or a solo worker's reviewer), and until that yes
nothing changes: the cash clock keeps the days Tim always said.

The rules file names its copy and the copy's sha, so ``policy watch`` sees
any change to the words, and each rule quotes the sentence it came from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from string import Template

from ..policy import money_words

TEMPLATE = Path(__file__).resolve().parent / "accountable-plan.txt"
RULES_FILE = "accountable-plan.toml"
COPY_FILE = "accountable-plan.txt"

ADOPTED_BY = {"nonprofit-solo": "reviewer", "nonprofit-small": "board"}
"""Who adopts the plan in each shape that gets one, by the shape's own role
name; where the ministry calls it something else, setup takes the role
someone there holds that reviews or confirms (#583). An organization (a
sending agency) keeps its own handbook; a business has no plan."""

ACCOUNT_DAYS, RETURN_DAYS, RECEIPT_FROM = 60, 120, Decimal("75")
"""The plan's days and receipt line, the ones the IRS's safe harbor names."""


@dataclass(frozen=True)
class Written:
    rules: str  # accountable-plan.toml
    words: str  # accountable-plan.txt


def write(ministry: str, *, cash: Decimal, covers: list[str], adopted_by: str) -> Written:
    """The plan's words and its rules for ``ministry``: the cash a Worker
    may hold, whom it covers (roles), and the role that adopts it."""
    words = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        ministry=ministry,
        account_days=ACCOUNT_DAYS,
        return_days=RETURN_DAYS,
        receipt_from=money_words(RECEIPT_FROM),
        cash_ceiling=money_words(cash),
    )
    sha = hashlib.sha256(words.encode("utf-8")).hexdigest()
    who = json.dumps(covers)
    rules = [
        (
            "cash-on-hand",
            f'cash_up_to = "{cash:.2f}"',
            f"Cash on hand stays at or under {money_words(cash)} unless a director approves more.",
            "5",
            f"Cash on hand is not to exceed the equivalent of {money_words(cash)} without the "
            "approval of a director.",
        ),
        (
            "account-within",
            f"account_within = {ACCOUNT_DAYS}",
            f"Every expense is accounted for within {ACCOUNT_DAYS} days of paying it.",
            "4",
            f"Within {ACCOUNT_DAYS} days after paying an expense, the Worker accounts for it",
        ),
        (
            "return-within",
            f"return_within = {RETURN_DAYS}",
            f"Cash not accounted for goes back to the ministry within {RETURN_DAYS} days "
            "of its monthly statement.",
            "6",
            f"is returned to a ministry account within {RETURN_DAYS} days after that statement",
        ),
        (
            "receipt-from",
            f'over = "{RECEIPT_FROM - Decimal("0.01"):.2f}"\nneeds = {{ receipt = true }}',
            f"A receipt is needed for any expense of {money_words(RECEIPT_FROM)} or more.",
            "4",
            f"A receipt is required for any single expense of {money_words(RECEIPT_FROM)} or more",
        ),
    ]
    body = "".join(
        f"\n[[rule]]\nid = {json.dumps(rid)}\nwho = {who}\n{setting}\n"
        f"says = {json.dumps(says)}\n"
        f"source = {{ section = {json.dumps(f'Section {section}')}, "
        f"quote = {json.dumps(quote)} }}\n"
        for rid, setting, says, section, quote in rules
    )
    head = (
        "# The ministry's accountable plan, written at setup (core/onboarding/accountable.py).\n"
        "# Its rules go in force together on one yes from the role named below, never\n"
        "# from anyone it covers. Change a number here and in the plan's words together;\n"
        "# the changed plan is a new card.\n"
        'layer = "tenant"\n'
        f"document = {json.dumps(ministry + ' accountable plan')}\n"
        "plan = true\n"
        f"adopted_by = {json.dumps(adopted_by)}\n\n"
        f"[copy]\nfile = {json.dumps(COPY_FILE)}\nsha = {json.dumps(sha)}\n"
    )
    return Written(rules=head + body, words=words)


def place(directory: Path, written: Written) -> None:
    """Into the ministry's rules folder, beside any rules it already has."""
    folder = directory / "rules"
    folder.mkdir(exist_ok=True)
    (folder / COPY_FILE).write_text(written.words, encoding="utf-8")
    (folder / RULES_FILE).write_text(written.rules, encoding="utf-8")


__all__ = ["ADOPTED_BY", "COPY_FILE", "RULES_FILE", "Written", "place", "write"]
