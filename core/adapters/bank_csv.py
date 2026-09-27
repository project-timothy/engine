"""Bank-data adapter: CSV import only (Phase 2; no bank API).

The CSV column layout and date format are tenant configuration
(``[bank_csv]`` in tenant.toml), because every bank exports differently. The
adapter normalizes to :class:`BankLine` records with ISO dates and Decimal
amounts; downstream verification consumes those, never the raw CSV.

The cleared-transactions CSV is one of three verification sources and is
KNOWN to be incomplete on its own: scheduled/in-process bill-pay payments do
not appear in it (the 2026-05-21 gap). Nothing in this module may be used as
the sole payment-status source.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel


class BankCsvFormat(BaseModel):
    """Column names and date format of a tenant's bank CSV export."""

    date_col: str = "Date"
    description_col: str = "Description"
    check_number_col: str = "Check Number"
    amount_col: str = "Amount"
    date_format: str = "%m/%d/%Y"
    # Where the tenant's statements land (phase 7 rows 7.3 and 7.4). The
    # daily run hands the whole folder to ``ap/reconcile`` as ``--param
    # statement_dir``; empty, or an empty folder, means no statement tier
    # that morning. A location, so it lives in tenant config, never core.
    statement_dir: str = ""
    # Which PDFs in that folder are this account's statements. A statement
    # folder collects strays (a card statement, a drawing somebody dropped
    # there), so the tenant narrows the glob to its own file naming; the
    # default is loose and leans on the marker below to skip the rest.
    statement_pdf_glob: str = "*.pdf"
    # Text the bank prints on every statement, under the check-grid header.
    # A glob-matched PDF without it and without a section header is some
    # other document: named in the log, skipped, never an error.
    statement_pdf_marker: str = "Indicates gap in check sequence"
    # The earliest date that can be clearing evidence (ISO, empty = none).
    # A statement whose period ends before it is never opened and any line
    # before it is dropped: a sealed year's books are closed, and re-reading
    # them can only re-litigate answers the close already gave.
    statement_floor: str = ""


class BankLine(BaseModel):
    date: str  # ISO YYYY-MM-DD
    description: str = ""
    check_ref: str = ""  # empty for ACH/card lines
    amount: Decimal


class BankCsvError(ValueError):
    pass


def parse_bank_csv(path: str | Path, fmt: BankCsvFormat) -> list[BankLine]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no bank CSV at {path}")
    lines: list[BankLine] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = {fmt.date_col, fmt.amount_col} - set(reader.fieldnames or [])
        if missing:
            raise BankCsvError(
                f"CSV at {path} lacks expected column(s) {sorted(missing)}; "
                f"header is {reader.fieldnames}"
            )
        for n, row in enumerate(reader, start=2):
            raw_date = (row.get(fmt.date_col) or "").strip()
            raw_amount = (row.get(fmt.amount_col) or "").strip().replace(",", "")
            try:
                iso = datetime.strptime(raw_date, fmt.date_format).date().isoformat()
            except ValueError as exc:
                raise BankCsvError(f"{path}:{n}: unparseable date {raw_date!r}") from exc
            try:
                amount = Decimal(raw_amount)
            except InvalidOperation as exc:
                raise BankCsvError(f"{path}:{n}: unparseable amount {raw_amount!r}") from exc
            lines.append(
                BankLine(
                    date=iso,
                    description=(row.get(fmt.description_col) or "").strip(),
                    check_ref=(row.get(fmt.check_number_col) or "").strip(),
                    amount=amount,
                )
            )
    return lines


def to_cleared_bank_state(lines: list[BankLine]) -> list[dict]:
    """Shape bank lines into the verification state's ``cleared_bank`` list."""
    return [{"check_ref": ln.check_ref, "amount": ln.amount, "date": ln.date} for ln in lines]


def amount_cents(line: BankLine) -> int:
    """Signed integer cents through Decimal, never float math. Money out is
    negative in every export the adapter reads (the bank's own sign)."""
    return int((line.amount * 100).to_integral_value())


def line_identity(line: BankLine) -> str:
    """Stable identity of one statement line: ``stmt:`` + a digest of the
    posted date, the signed cents, and the check reference. The same line in
    a later, longer export is the same line, so a run that already explained
    it (settled, parked, or flagged) never explains it twice; a different
    amount or number is a different line. Bank text (the description) is not
    part of it: banks reword memos between exports."""
    raw = f"{line.date}|{amount_cents(line)}|{line.check_ref.strip()}"
    return "stmt:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
