"""A receipt's category from the person's own words (the owner's decisions of
2026-10-13). Code, never a model: "4,300 rupees for the bus to Pokhara" is
Travel because "bus" is a travel word, and only when the ministry's own list
has a Travel. Words that name the category itself win ("put it under
Medical"); words that point two ways, or nowhere, leave it uncategorized, and
the reviewer places it. Nothing here is a tenant's: the ministry's list is its
own config ([expenses.category_accounts])."""

from __future__ import annotations

import re

UNCATEGORIZED = "Uncategorized"

# Starter-list words. A category a ministry renames or drops simply never
# matches; its own names still match when said outright.
WORDS: dict[str, frozenset[str]] = {
    "Travel": frozenset(
        "bus buses taxi cab fuel petrol diesel flight flights airfare plane train fare fares "
        "uber rickshaw tuk-tuk jeep boat ferry toll parking visa".split()
    ),
    "Lodging": frozenset("hotel guesthouse hostel lodge lodging motel airbnb".split()),
    "Meals": frozenset(
        "lunch dinner breakfast meal meals food restaurant cafe coffee groceries".split()
    ),
    "Communication": frozenset("phone sim airtime data internet wifi postage".split()),
    "Medical": frozenset(
        "doctor clinic hospital pharmacy medicine medicines prescription dental".split()
    ),
    "Housing": frozenset("rent electricity utilities utility".split()),
    "Ministry Supplies": frozenset(
        "bible bibles tracts printing printer ink toner paper pens pencils notebooks "
        "stationery chalk markers curriculum projector supplies books".split()
    ),
}


def canonical(category: str, categories: list[str]) -> str | None:
    """The ministry's own spelling of ``category``, "" for uncategorized, or
    None when the ministry has no such category."""
    said = category.strip().casefold()
    if said in ("", UNCATEGORIZED.casefold()):
        return ""
    return next((c for c in categories if c.casefold() == said), None)


def from_words(said: str, categories: list[str]) -> str:
    """The one category the words point to, or "" when they point to none or
    to more than one."""
    text = said.casefold()
    named = [c for c in categories if re.search(rf"\b{re.escape(c.casefold())}\b", text)]
    if len(named) == 1:
        return named[0]
    if named:
        return ""
    words = set(re.findall(r"[a-z][a-z-]*", text))
    hits = [c for c in categories if WORDS.get(c, frozenset()) & words]
    return hits[0] if len(hits) == 1 else ""
