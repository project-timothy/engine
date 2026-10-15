"""The photo reader seam: what a receipt's photo says it cost (board item
t-receipt-photo, 2026-10-13).

A reading is a proposal and nothing more. The person's own words decide a
receipt's amount, either by saying it or by a yes to the reading; a reader
never prices a receipt on its own (engine invariant 2). The reader that
runs on a ministry's box is that ministry's choice; ``reader = ""`` (none)
keeps Tim asking how much. ``core/adapters/pretend_reader.py`` fits this
shape for practice and tests.

Shapes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Reading:
    """What the photo says was paid: ``amount`` an exact decimal string
    ("4300.00"), ``currency`` an ISO code ("NPR"). A reader that can't tell
    the currency returns no reading at all, never a guessed one."""

    amount: str
    currency: str


@runtime_checkable
class ReceiptReader(Protocol):
    def read(self, photo: bytes) -> Reading | None:
        """The amount on the receipt in ``photo``, or None when it can't say."""
        ...
