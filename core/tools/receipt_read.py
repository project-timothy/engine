"""A receipt read the moment it lands (Standalone Tim, card R1).

Until now a receipt photo's amount was read by the person's own Claude or
ChatGPT, in a second step (``receipts_to_read``, ``record_reading``) that
many never take. When a ministry's file switches it on, the receipt page
reads the photo itself as it arrives and says it back in one plain line:
"Shoprite, November 12, 4,300 rupees. Right?". Her tap is the yes.

What stays exactly as it was:

- **A reading is a proposal.** It is recorded the way ``record_reading``
  records one (``receipts.record_read``); the amount counts only when she
  taps that it's right (``receipts.agree_read``, the same yes
  ``agree_to_readings`` gives). No model output writes a money value
  (engine invariant 2).
- **Off unless the ministry's file names it.** The job type is
  ``receipt_read``, switched on only by its own line in ``[llm.jobs]``
  (``core.llm.policy.NAMED_ONLY``: a ``default`` tier never covers it), and
  held by its own eval set (``core/llm/eval_sets/receipt_read``, 40 made-up
  receipts). With no line, a bare ``fixture`` tier, a failed or refused call,
  a timeout, or the month's cap reached, :func:`read` returns None and the
  page answers exactly as it did before.
- **The reader is the expense lane's own.** The prompt and the reply shape
  are ``core.agents.ap.extraction``'s (one extractor serves invoices and
  receipts), through its :class:`GatewayExtractor`, plus one field: the
  currency the total is printed in, which a missionary's receipts need and
  the nightly jobs never asked for. The nightly ``receipt_extract`` job is
  untouched and keeps its own line.
- **The ledger is never held across the call.** The page runs outside any
  job, so the call is priced, recorded, and capped through
  ``complete_metered`` and the talk :class:`~core.talk.meter.Meter`, the way
  Tim's own voice is.

Boundary classification (docs/boundary-rules.md): the model reads a photo
into fields; code checks every field, compares the amount with what she
said, and writes nothing that counts until her tap.
"""

from __future__ import annotations

import re
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import Field

from ..agents.ap.extraction import ExtractionReply, GatewayExtractor
from ..llm.policy import named_on

JOB = "receipt_read"
"""The job type a ministry's ``[llm.jobs]`` names to switch the reader on."""

TIMEOUT_S = 45
"""How long the page waits on the model before it answers the way it always
has. Seconds on a phone; the photo is saved either way."""

READABLE = (".jpg", ".png", ".pdf")
"""What the reader is shown: a photo or a PDF. A HEIC photo is filed and
asked about the way it always was."""

CURRENCY = """\
Also reply with key:
  currency: the ISO 4217 code of the money the total is printed in (USD, EUR,
    NPR, KES, MZN, ...), read from the symbol or code printed beside it, or
    from the country the receipt plainly comes from; null if nothing on the
    receipt tells you
For invoice_date: the date the receipt prints. One exception: a ticket whose
only date is labeled as the day of travel (Journey, Travel date, Reisedatum)
doesn't print the day it was paid, so reply null there. A bill date, a plain
date, and a hotel's departure date are each the receipt's day.
"""
"""The lines the receipt page adds to the extractor's brief: the currency, and
a ticket's travel date never taken for the day it was paid (the owner's call
of 2026-11-19: Tim asks her the day)."""

WORDS_FILE = Path(__file__).with_name("receipt_read.toml")


class PageReading(ExtractionReply):
    """The extractor's reply, with the currency the receipt prints."""

    currency: str | None = Field(default=None, max_length=8)


@dataclass(frozen=True)
class Reading:
    """What one photo reads, checked by code: an amount over zero in a
    currency the engine knows, the day if it is one that has come, who it
    reads as paid, and the category word if it gave one."""

    amount: Decimal
    currency: str
    day: date | None = None
    vendor: str = ""
    category: str = ""

    def tag(self) -> str:
        """The reading as an amount tag ("$43.00", "NPR4300.00")."""
        if self.currency == "USD":
            return f"${self.amount:.2f}"
        return f"{self.currency}{self.amount:.2f}"

    def agrees(self, amount: Decimal, currency: str) -> bool:
        """Whether what she said is what the receipt prints."""
        return self.currency == currency and self.amount == amount

    def words(self, today: date) -> str:
        """Who, the day, and how much, the way she'd say it."""
        parts = [self.vendor] if self.vendor else []
        if self.day is not None:
            when = f"{self.day:%B} {self.day.day}"
            parts.append(when if self.day.year == today.year else f"{when}, {self.day.year}")
        parts.append(money(self.currency, self.amount))
        return ", ".join(parts)


def on(settings: Any) -> bool:
    """Whether the ministry's file switches the reader on: ``receipt_read``
    named in ``[llm.jobs]`` with a tier."""
    return named_on(settings, JOB)


def read(
    photo: bytes,
    suffix: str,
    *,
    settings: Any,
    ledger_root: str | Path,
    tenant: str,
    person: str,
    today: date,
    adapter: Any = None,
    timeout_s: int = TIMEOUT_S,
) -> Reading | None:
    """What the photo reads, or None whenever the answer can't be trusted to
    show her: the reader is off, the call failed, was refused, timed out, or
    hit the month's cap, or the reply names no amount, no known currency,
    or is not a receipt. ``adapter`` is for tests and the proving ground's
    scripted model; the ministry's tier is used otherwise."""
    from ..llm.policy import complete_metered
    from ..talk.meter import Meter

    if not on(settings) or suffix not in READABLE:
        return None
    meter = Meter(ledger_root)

    def metered(_ctx: Any, job_type: str, messages: list, output_model: Any, **more: Any):
        return complete_metered(
            settings,
            meter,
            job_type,
            messages,
            output_model,
            tenant=tenant,
            run_key=f"{JOB}:{person}:{today.isoformat()}",
            redact_detail=True,  # a failed reply may quote what the receipt says
            **more,
        )

    reader = GatewayExtractor(
        None,
        job_type=JOB,
        timeout_s=timeout_s,
        adapter=adapter,
        completer=metered,
        output_model=PageReading,
        also=CURRENCY,
    )
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"receipt{suffix}"
        path.write_bytes(photo)
        try:
            reply = reader.read(path)
        except Exception:  # noqa: BLE001  any failure: the page answers as it always has
            return None
    return checked(reply, today)


def checked(reply: Any, today: date) -> Reading | None:
    """The reply as a :class:`Reading`, or None. Code decides every field."""
    from .receipts import _CODES

    if reply.doc_type not in ("receipt", "invoice") or reply.amount is None:
        return None
    try:
        amount = Decimal(reply.amount).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    currency = str(getattr(reply, "currency", "") or "").strip().upper()
    if amount <= 0 or currency not in _CODES:
        return None
    try:
        day: date | None = date.fromisoformat(str(reply.invoice_date or ""))
    except ValueError:
        day = None
    if day is not None and day > today:
        day = None  # a day still to come is a misreading
    vendor = " ".join(str(reply.vendor_name or "").split())[:60]
    if not re.search(r"[^\W\d_]", vendor):
        vendor = ""
    return Reading(amount, currency, day, vendor, str(reply.category or "").strip()[:40])


_PLURAL = {
    "NPR": "rupees",
    "INR": "rupees",
    "PKR": "rupees",
    "LKR": "rupees",
    "MZN": "meticais",
    "KES": "shillings",
    "UGX": "shillings",
    "TZS": "shillings",
    "MXN": "pesos",
    "PHP": "pesos",
    "NGN": "naira",
    "GHS": "cedis",
    "ZMW": "kwacha",
    "MWK": "kwacha",
    "ETB": "birr",
    "ZAR": "rand",
    "THB": "baht",
    "KHR": "riel",
    "PEN": "soles",
    "GTQ": "quetzales",
    "HNL": "lempiras",
    "BDT": "taka",
}
_SIGNS = {"USD": "$", "EUR": "€", "GBP": "£"}


def money(currency: str, amount: Decimal) -> str:
    """An amount the way a person says it: "$9", "$12.40", "4,300 rupees",
    "€18.50", "CHF 40"."""
    figure = f"{amount:,.2f}".removesuffix(".00")
    if currency in _SIGNS:
        return f"{_SIGNS[currency]}{figure}"
    if currency in _PLURAL:
        return f"{figure} {_PLURAL[currency]}"
    return f"{currency} {figure}"


@cache
def words() -> dict[str, str]:
    """The page's lines for a reading (``receipt_read.toml``)."""
    return dict(tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))["words"])


__all__ = ["JOB", "PageReading", "Reading", "checked", "money", "on", "read", "words"]
