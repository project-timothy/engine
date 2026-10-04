"""AR agent: the remittance-advice shape and its body-only parser.

A customer's remittance advice is the earliest thing that knows money is
coming, and it arrives as mail with NO attachment: the numbers live in two
tables in the message body. The AP mail feed cannot see it at all, because
that feed lists messages that carry attachments and saves files.

This module is the pure parse seam. It takes a subject and a body and
returns a :class:`Remittance`, or raises. It knows nothing about a mailbox,
a ledger, or a customer: the sender and the subject marker that decide which
messages reach it are tenant configuration.

**A single-payer reference parser.** The two grids below are one payer's
advice layout (an ERP-style label/value grid and an invoice grid with
``DD-MON-YYYY`` dates), the first tenant's primary customer's. A payer that
writes its advice differently is a second parser beside this one, never a
loosening of this one, and a body this parser cannot read raises rather than
guessing.

Two tables, in the order the payer writes them:

1. a label/value grid: ``Payment made to``, ``Our Supplier No.``,
   ``Supplier site name``, ``Payment number``, ``Payment date``,
   ``Payment currency``, ``Payment amount``;
2. the invoice grid: ``Invoice Number``, ``Invoice Date``,
   ``Invoice Description``, ``Discount Taken``, ``Amount Paid``,
   ``Amount Remaining``, one row per document the payment settles. A credit
   memo rides the same grid with a negative Amount Paid, which is why the
   rows tie to the payment amount and the invoice total does not.

Money is read as integer cents by code, never by a model (invariant 2). A
body this parser cannot read raises :class:`RemittanceNotRecognized`, and
the job turns that into an anomaly: a payer that changes its layout must be
loud, because the alternative is money nobody records.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from pydantic import BaseModel, Field

DEFAULT_SUBJECT_MARKER = "Remittance Advice"

# The payment number in the subject, the fallback when the body's label/value
# grid is not where this payer keeps it.
_SUBJECT_PAYMENT = re.compile(r"payment\s*#\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE)

# ``15-SEP-2026``: the shape the payer's accounting system prints.
_DDMONYYYY = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_MONTHS = {
    month: number
    for number, month in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}

# The invoice grid's column labels, lowercased. A row is only read when the
# header row above it named the columns: nothing is read by position alone.
INVOICE_NUMBER = "invoice number"
INVOICE_DATE = "invoice date"
INVOICE_DESCRIPTION = "invoice description"
AMOUNT_PAID = "amount paid"
AMOUNT_REMAINING = "amount remaining"
INVOICE_COLUMNS = (
    INVOICE_NUMBER,
    INVOICE_DATE,
    INVOICE_DESCRIPTION,
    "discount taken",
    AMOUNT_PAID,
    AMOUNT_REMAINING,
)

# The label/value grid, lowercased and stripped of its colon.
PAYMENT_NUMBER = "payment number"
PAYMENT_DATE = "payment date"
PAYMENT_AMOUNT = "payment amount"
PAYMENT_CURRENCY = "payment currency"
PAYMENT_ACCOUNT = "payment made to"
SUPPLIER_NO = "our supplier no."


class RemittanceNotRecognized(ValueError):
    """The message is not a remittance advice this parser can read."""


class RemittanceInvoice(BaseModel):
    """One row of the invoice grid. ``amount_paid_cents`` is negative for a
    credit memo the payment netted out."""

    invoice_number: str
    invoice_date: str = ""
    description: str = ""
    amount_paid_cents: int = 0
    amount_remaining_cents: int = 0


class Remittance(BaseModel):
    """One remittance advice, as the payer wrote it."""

    payment_number: str
    payment_date: str = ""
    amount_cents: int = 0
    currency: str = "USD"
    # The payer's own name for the account it paid: the tenant's supplier
    # record. It names the customer, which is what the AR register needs.
    account: str = ""
    supplier_no: str = ""
    invoices: list[RemittanceInvoice] = Field(default_factory=list)


class RemittanceMessage(BaseModel):
    """A mailbox message the job may parse: metadata plus the body. Fixture
    files carry the same shape the live listing builds."""

    id: str
    sender: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""


# ---- predicates (the gates, both tenant-configured) --------------------------


def subject_is_remittance(subject: str, marker: str = DEFAULT_SUBJECT_MARKER) -> bool:
    """True when the subject carries the marker (case-insensitive). An empty
    marker matches nothing: a tenant that blanks it turns the lane off."""
    needle = (marker or "").strip().lower()
    return bool(needle) and needle in (subject or "").lower()


def sender_matches(sender: str, senders: list[str]) -> bool:
    """True when the sender matches any configured entry (address or domain
    substring, case-insensitive). An EMPTY list matches every sender: the
    subject marker is then the only gate, which is what lets a second payer's
    advice be read the day it first arrives instead of a month later."""
    if not [s for s in senders if s.strip()]:
        return True
    low = (sender or "").lower()
    return any(token.strip().lower() in low for token in senders if token.strip())


# ---- the parse ---------------------------------------------------------------


class _TableRows(HTMLParser):
    """Every table row in the body, as a list of cell strings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def table_rows(body: str) -> list[list[str]]:
    """The body's table rows, in document order."""
    parser = _TableRows()
    parser.feed(body or "")
    parser.close()
    return parser.rows


def cents(raw: str) -> int:
    """``58,250.00`` -> 5825000. Empty is zero; a parenthesized or
    minus-signed amount is negative (a credit memo). Anything that is not an
    amount raises rather than guessing: no money value is ever inferred."""
    text = (raw or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return 0
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if text.startswith("-"):
        negative, text = True, text[1:]
    if not text:
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise RemittanceNotRecognized(f"{raw!r} is not an amount") from exc
    whole = int((value * 100).to_integral_value())
    return -whole if negative else whole


def iso_date(raw: str) -> str:
    """``15-SEP-2026`` -> ``2026-09-15``. An ISO date passes through; any
    other shape is returned as written, because a date the parser cannot
    read is still the payer's own record and must not be invented."""
    text = (raw or "").strip()
    found = _DDMONYYYY.match(text)
    if not found:
        return text
    month = _MONTHS.get(found.group(2).lower())
    if month is None:
        return text
    return f"{int(found.group(3)):04d}-{month:02d}-{int(found.group(1)):02d}"


def _fields(rows: list[list[str]]) -> dict[str, str]:
    """The label/value grid: the FIRST value wins, so the invoice grid's own
    two-cell rows (if a payer ever prints one) cannot overwrite a header."""
    fields: dict[str, str] = {}
    for row in rows:
        if len(row) != 2:
            continue
        label = row[0].strip().rstrip(":").strip().lower()
        if label and label not in fields:
            fields[label] = row[1].strip()
    return fields


def _invoices(rows: list[list[str]]) -> list[RemittanceInvoice]:
    """The invoice grid: the header row names the columns, and the rows of
    the same width beneath it are the documents this payment settled."""
    for position, row in enumerate(rows):
        lowered = [cell.strip().lower() for cell in row]
        if INVOICE_NUMBER not in lowered or AMOUNT_PAID not in lowered:
            continue
        index = {name: lowered.index(name) for name in INVOICE_COLUMNS if name in lowered}
        width = len(row)
        lines: list[RemittanceInvoice] = []
        for candidate in rows[position + 1 :]:
            if len(candidate) != width:
                break
            cell = {name: candidate[at].strip() for name, at in index.items()}
            number = cell.get(INVOICE_NUMBER, "")
            paid = cell.get(AMOUNT_PAID, "")
            if not number and not paid:
                continue
            lines.append(
                RemittanceInvoice(
                    invoice_number=number,
                    invoice_date=iso_date(cell.get(INVOICE_DATE, "")),
                    description=cell.get(INVOICE_DESCRIPTION, ""),
                    amount_paid_cents=cents(paid),
                    amount_remaining_cents=cents(cell.get(AMOUNT_REMAINING, "")),
                )
            )
        return lines
    return []


def parse_remittance(*, subject: str, body: str) -> Remittance:
    """One advice, body only. Raises :class:`RemittanceNotRecognized` when
    the body carries no payment number or no payment amount, the two facts
    the lane exists to record."""
    rows = table_rows(body)
    if not rows:
        raise RemittanceNotRecognized(
            "the body carries no table: this is not a remittance advice, or the payer "
            "changed its layout"
        )
    fields = _fields(rows)
    payment_number = fields.get(PAYMENT_NUMBER, "").strip()
    if not payment_number:
        found = _SUBJECT_PAYMENT.search(subject or "")
        payment_number = found.group(1) if found else ""
    if not payment_number:
        raise RemittanceNotRecognized("no payment number in the body table or the subject")
    if PAYMENT_AMOUNT not in fields:
        raise RemittanceNotRecognized(
            f"payment {payment_number}: the body carries no {PAYMENT_AMOUNT!r} row"
        )
    return Remittance(
        payment_number=payment_number,
        payment_date=iso_date(fields.get(PAYMENT_DATE, "")),
        amount_cents=cents(fields[PAYMENT_AMOUNT]),
        currency=fields.get(PAYMENT_CURRENCY, "USD") or "USD",
        account=fields.get(PAYMENT_ACCOUNT, ""),
        supplier_no=fields.get(SUPPLIER_NO, ""),
        invoices=_invoices(rows),
    )
