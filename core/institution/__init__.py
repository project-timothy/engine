"""The institution profile: how a sending organization's missionaries are
served (Robb's idea 1, decided 2026-10-13; [institution] in tenant.toml).

Whether Tim places receipts, where claims go after the missionary's yes, and
the mileage rate. Sending a claim is the one place an institution's own
system can fail under a missionary; ``submit`` turns every failure into a
held claim and one plain sentence, so nobody ever reads a status code."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..contracts.institution import Claim, ClaimSink, InstitutionDown
from ..engine.config import InstitutionSettings

SENT, HELD = "sent", "held"

# The IRS standard mileage rate for business use, by the day it took
# effect. Add a row when the IRS announces one; a drive is priced at the
# rate in force on its own day. [NEEDS REVIEW] a mid-2026 change is
# reported (76 cents from 2026-07-01) but not confirmed on IRS.gov.
IRS_MILEAGE: tuple[tuple[date, Decimal], ...] = (
    (date(2025, 1, 1), Decimal("0.70")),
    (date(2026, 1, 1), Decimal("0.725")),
)

SINKS = ("pretend",)


class InstitutionError(ValueError):
    """A profile the engine can't serve: an unknown destination, or a day no
    rate covers."""


@dataclass(frozen=True)
class Outcome:
    receipt: str
    status: str  # sent | held
    reference: str = ""
    say: str = ""  # for the person, in plain words, when it was held


def mileage_rate(settings: InstitutionSettings, day: date) -> tuple[Decimal, str]:
    """The rate per mile for a drive on ``day`` and whose rate it is."""
    if settings.mileage_rate is not None:
        return settings.mileage_rate, settings.name or "the ministry"
    rates = [rate for start, rate in IRS_MILEAGE if start <= day]
    if not rates:
        raise InstitutionError(f"no IRS rate on file for {day.isoformat()}")
    return rates[-1], "IRS"


def claim_sink(settings: InstitutionSettings, *, store: Path | None) -> ClaimSink | None:
    """Where this ministry's claims go: None for its own books."""
    if not settings.submit_to:
        return None
    if settings.submit_to == "pretend":
        from ..adapters.pretend_institution import PretendInstitution

        if store is None:
            raise InstitutionError("submit_to 'pretend' needs a folder to keep its claims in")
        return PretendInstitution(store)
    raise InstitutionError(
        f"[institution] submit_to {settings.submit_to!r}: "
        f"use one of {', '.join(SINKS)}, or leave it empty"
    )


def held_words(institution: str) -> str:
    who = institution or "The ministry's office"
    return (
        f"{who} isn't answering right now. "
        f"Tim is holding this receipt and will send it when {who} is back."
    )


def submit(sink: ClaimSink, claims: Sequence[Claim], *, institution: str) -> list[Outcome]:
    """Send the claims. Anything the institution doesn't take, for any
    reason, is held for the next try, with the same plain sentence."""
    try:
        taken = {t.receipt: t.reference for t in sink.submit(claims)}
    except InstitutionDown:
        taken = {}
    except Exception:  # noqa: BLE001  a stranger's failure is held, never shown
        taken = {}
    return [
        Outcome(c.receipt, SENT, taken[c.receipt])
        if taken.get(c.receipt)
        else Outcome(c.receipt, HELD, "", held_words(institution))
        for c in claims
    ]


__all__ = [
    "HELD",
    "IRS_MILEAGE",
    "SENT",
    "InstitutionError",
    "Outcome",
    "claim_sink",
    "held_words",
    "mileage_rate",
    "submit",
]
