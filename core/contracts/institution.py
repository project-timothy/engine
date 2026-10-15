"""The institution seam: where a missionary's claims go when their sending
organization keeps its own books (Robb's idea 1, 2026-10-13).

A small church or an independent missionary needs none of this: Tim places
each receipt in the ministry's categories and the books pay it out. An
institution with its own system (AGWM, whose receipts land in Sage, which
does the categorizing) takes the claims instead, after the missionary's yes.
``core/adapters/pretend_institution.py`` fits this shape until an
institution's real interface is in hand.

Shapes only. A claim carries what an expense claim needs and nothing more:
no category (the institution's own system places it), no account, no card.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Claim:
    """One receipt, as the institution receives it. Money is an exact decimal
    string. ``amount`` and ``currency`` are what was paid ("4300.00", "NPR");
    ``usd`` is its dollar value on its own day when the engine has priced it,
    else "". ``said`` is the missionary's own words; ``photo`` the receipt
    image's path as the engine filed it."""

    receipt: str
    person: str
    day: str
    amount: str
    currency: str
    usd: str
    said: str
    photo: str


@dataclass(frozen=True)
class Taken:
    """What the institution said back for one claim: its own reference."""

    receipt: str
    reference: str


class InstitutionDown(RuntimeError):
    """The institution can't take claims right now (down, busy, or refusing
    the sign-in). The caller holds the claims and tries again; the person is
    told in plain words, never this exception's text."""


@runtime_checkable
class ClaimSink(Protocol):
    """An institution's door for claims."""

    def submit(self, claims: Sequence[Claim]) -> list[Taken]:
        """Send the claims; one ``Taken`` per claim it accepted. Raises
        ``InstitutionDown`` when it can take none of them now. ``receipt`` is
        the claim's identity: a claim sent again (a run that died before it
        recorded the answer) is answered with its first reference, never
        taken twice."""
        ...

    def status(self, reference: str) -> str:
        """Where one claim stands in the institution's own words
        ("received", "approved", "paid"), or "" when it can't say."""
        ...
