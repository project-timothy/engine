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


def irs_mileage_rate(day: date) -> Decimal:
    """The IRS standard rate per mile for business use on ``day``, from the
    year's tax table (``core.tax.mileage``)."""
    from ..tax import figures, mileage

    try:
        return mileage.irs_rate(day)
    except figures.NoYear as gone:
        raise InstitutionError(
            f"Tim doesn't hold the IRS mileage rate for {gone.year} yet."
        ) from gone


def mileage_rate(settings: InstitutionSettings, day: date) -> tuple[Decimal, str]:
    """The rate a drive on ``day`` is paid back at and whose rate it is: the
    ministry's when it sets one, else the IRS rate. A ministry rate above
    the IRS rate that day is refused (c34 PR 6): the excess counts as pay."""
    irs = irs_mileage_rate(day)
    own = settings.mileage_rate
    if own is None:
        return irs, "IRS"
    who = settings.name or "The ministry"
    if own > irs:
        raise InstitutionError(
            f"{who} pays drives back at {_cents(own)} a mile, above the IRS rate of "
            f"{_cents(irs)} on {day:%b} {day.day}, {day.year}. Tim pays a drive back at the "
            "IRS rate or less, because anything above it counts as pay. Lower the "
            "ministry's mileage rate to the IRS rate or below, and the drive is priced."
        )
    return own, settings.name or "the ministry"


def _cents(rate: Decimal) -> str:
    """72.5 cents, 74 cents."""
    return f"{(rate * 100).normalize():f} cents"


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
    "SENT",
    "InstitutionError",
    "Outcome",
    "claim_sink",
    "held_words",
    "irs_mileage_rate",
    "mileage_rate",
    "submit",
]
