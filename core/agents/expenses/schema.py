"""Contracts and deterministic parsers for the expenses agent.

``RECEIPT_SUFFIXES`` is THE single shared definition of "a file that can be
a receipt" (docs/expenses-design.md §1); the closer's check 8 imports it from
here so the two can never drift.

The filename parsers are the deterministic half of extraction: a project
tag, a dollar amount, or an explicit owner tag written into a filename is
ground truth the LLM proposal must agree with, and disagreement flags for
review instead of silently choosing either side (invariant 2).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# The one shared receipt-suffix set (design: "single shared definition").
RECEIPT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".heic"}

# A project tag anywhere in a filename or folder name: P00_0102, PN00_0101,
# P00-0101, or the short P2035 form the July live case used. Matched exactly,
# never fuzzily; an unmatched name simply carries no tag.
_PROJECT_TAG = re.compile(r"\bP N?(?:\d{2}[_-])?\d{4}\b".replace(" ", ""))

# A dollar amount written into a filename: "$43.87", "$1,234.56".
_AMOUNT_TAG = re.compile(r"\$(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})")

# The owner's explicit entertainment tag (design: Entertainment is NEVER
# assumed; it exists only when the owner writes it). Case-insensitive token.
_ENTERTAINMENT_TAG = re.compile(r"(?:^|[\s_\-(\[])ENT(?:ERTAINMENT)?(?:$|[\s_\-)\].])", re.I)

# Categories that mean "a meal" in a proposal; all fold to Meals (the 50%
# limitation is the CPA's year-end act, never applied at reimbursement time).
_MEAL_WORDS = {"meal", "meals", "food", "restaurant", "dining", "lunch", "dinner", "breakfast"}


def parse_project_tag(name: str) -> str:
    """The project tag written in a file or folder name, or ''."""
    m = _PROJECT_TAG.search(name)
    return m.group(0) if m else ""


def parse_amount_tag(name: str) -> int | None:
    """A dollar amount written in a filename, as integer cents, or None."""
    m = _AMOUNT_TAG.search(name)
    if not m:
        return None
    dollars = int(m.group(1).replace(",", ""))
    return dollars * 100 + int(m.group(2))


def has_entertainment_tag(name: str) -> bool:
    return bool(_ENTERTAINMENT_TAG.search(name))


def normalize_category(proposed: str | None, filename: str) -> str:
    """Apply the standing category rules to a proposal (owner, 2026-07-22).

    Meal-shaped proposals fold to ``Meals``. ``Entertainment`` is never
    accepted from a proposal: without the owner's explicit filename tag it
    downgrades to Meals (the reviewable, deductible default). An empty
    proposal stays empty — the review card asks, code never guesses.
    """
    raw = (proposed or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low in _MEAL_WORDS:
        return "Meals"
    if low == "entertainment":
        return "Entertainment" if has_entertainment_tag(filename) else "Meals"
    return raw.title() if low.islower() else raw


class ManifestLine(BaseModel):
    """One receipt-backed line as the manifest records it."""

    receipt_file: str
    receipt_sha256: str
    vendor: str = ""
    expense_date: str = ""
    amount_cents: int
    category: str = ""
    project: str = ""
    note: str = ""
    # Owner-directed split discriminator (issue #110): 0 = the whole
    # receipt (the default, keys unchanged); 1..N = one part of a split
    # (hotel-folio shape), keyed report:line:<sha>:<part>.
    part: int = 0


class ReportManifest(BaseModel):
    """What "consumed" means: the manifest written next to the package
    (docs/expenses-design.md §3). Check 8 and the janitor trust this file."""

    report_key: str
    tenant: str
    person: str
    month: str
    total_cents: int
    lines: list[ManifestLine] = Field(default_factory=list)
