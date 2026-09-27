"""Input/output contracts for the AP agent.

``ExtractedDocument`` is THE LLM boundary (invariant 2): whatever the
extractor returns must validate against this model before any code consumes
it. Downstream of validation, everything is deterministic: code converts the
Decimal amount to integer cents, code matches vendors, code writes the
ledger. The LLM classifies and extracts; it never computes or writes money.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

DocType = Literal[
    "invoice",
    "po",
    "quote",
    "statement",
    "receipt",
    "proposal",
    "reference",
    "unknown",
]

# Files with other suffixes are recorded as non-candidates without an
# extraction call; the landing folder carries CAD files, spreadsheets, video,
# and inline-image artifacts that are never AP paperwork.
AP_CANDIDATE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff"}


class ExtractedDocument(BaseModel):
    """What extraction asserts about one file. Validated before use."""

    doc_type: DocType = "unknown"
    vendor_name: str | None = None
    invoice_number: str | None = None
    amount: Decimal | None = None  # signed; negative preserved for credits
    invoice_date: str | None = None  # ISO YYYY-MM-DD
    due_date: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    needs_ocr: bool = False
    # Expense category proposal for purchase receipts (e.g. Meals,
    # Travel, Lodging, Fuel, Supplies). None for non-receipt documents. AP
    # intake ignores it; the expenses agent normalizes it against the
    # standing category rules before it ever reaches a review card.
    category: str | None = None


class BillpayEntry(BaseModel):
    """One row of a bill-pay queue snapshot (captured outside the engine)."""

    payee: str
    invoice_ref: str | None = None
    amount: Decimal | None = None
    state: str = "Scheduled"  # Scheduled | In Process


def amount_to_cents(amount: Decimal) -> int:
    """Exact Decimal-to-cents conversion. Raises if sub-cent precision appears."""
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount} has sub-cent precision")
    return int(cents)
