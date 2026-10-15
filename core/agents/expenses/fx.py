"""A foreign receipt's dollar value (alpha step 3, the owner's decisions of
2026-10-13).

Ruth pays 4,300 rupees in Kathmandu. Her words are kept as she said them;
the dollar value is set once, at the rate on the receipt's own date, from a
free daily feed that needs no key (fawazahmed0's currency-api, served from
jsDelivr with a Cloudflare mirror), and it never moves after that: the rate
is recorded as an event whose key is the currency and the day, so the first
rate recorded for a day is the rate for that day. Every line keeps the
rate, its date, and where it came from, so a reviewer can check any of
them. When the feed has no rate for the day yet, or is down, the line waits
("rate pending"); nothing is guessed. Code, never a model, does every step.

The feed's address can be pointed elsewhere with ``ENGINE_FX_FEED`` (a URL
template with ``{day}``; a proving-ground run uses a ``file://`` copy).
"""

from __future__ import annotations

import json
import os
import urllib.error
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from ...engine import web
from .schema import parse_foreign_claim

RATE_EVENT = "expense.fx_rate"

FEEDS = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{day}/v1/currencies/usd.min.json",
    "https://{day}.currency-api.pages.dev/v1/currencies/usd.min.json",
)
SOURCE = "currency-api (fawazahmed0)"
TIMEOUT = 10


@dataclass(frozen=True)
class Rate:
    """How many of ``currency`` one dollar bought on ``day``, as the feed
    said it (a decimal string, never a float)."""

    currency: str
    day: str
    per_usd: str
    source: str

    def cents(self, amount: Decimal) -> int:
        """The dollar value of ``amount`` in this currency, in cents."""
        dollars = (amount / Decimal(self.per_usd)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return int(dollars * 100)

    def key(self) -> str:
        return rate_key(self.currency, self.day)

    def payload(self) -> dict:
        return {
            "currency": self.currency,
            "day": self.day,
            "per_usd": self.per_usd,
            "source": self.source,
        }


def rate_key(currency: str, day: str) -> str:
    return f"fx:{currency}:{day}"


def from_payload(payload: dict) -> Rate | None:
    try:
        rate = Rate(
            str(payload["currency"]),
            str(payload["day"]),
            str(payload["per_usd"]),
            str(payload.get("source", "")),
        )
        if Decimal(rate.per_usd) <= 0:
            return None
    except (KeyError, InvalidOperation):
        return None
    return rate


def stored(events: list[dict]) -> dict[tuple[str, str], Rate]:
    """The rates already recorded, by (currency, day); the first one for a
    day stands."""
    out: dict[tuple[str, str], Rate] = {}
    for e in events:
        if e.get("event_type") != RATE_EVENT:
            continue
        rate = from_payload(e.get("payload", {}))
        if rate is not None:
            out.setdefault((rate.currency, rate.day), rate)
    return out


def _get(url: str) -> bytes:
    """The feed's day, over https (core/engine/web.py), or from a file copy
    when the host's ENGINE_FX_FEED names one (a proving-ground run)."""
    if url.startswith("file://"):
        return Path(urlsplit(url).path).read_bytes()
    req = web.request(url, headers={"User-Agent": "timothy-engine"})
    with web.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch(currency: str, day: str, *, get=_get) -> Rate | None:
    """The feed's rate for ``currency`` on ``day``, or None when no source
    has it (not published yet, unknown currency, or down)."""
    custom = os.environ.get("ENGINE_FX_FEED")
    for template in (custom,) if custom else FEEDS:
        url = template.format(day=day)
        try:
            body = json.loads(get(url), parse_float=Decimal)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if not isinstance(body, dict) or str(body.get("date")) != day:
            continue
        value = (body.get("usd") or {}).get(currency.lower())
        if not isinstance(value, Decimal | int) or value <= 0:
            continue
        return Rate(currency, day, str(value), SOURCE if not custom else template)
    return None


def needed(name: str) -> tuple[str, str] | None:
    """The (currency, day) a door receipt's name needs a rate for, or None."""
    claim = parse_foreign_claim(name)
    return None if claim is None else (claim[2], claim[0])


def priced(name: str, rates: dict[tuple[str, str], Rate]) -> dict:
    """A foreign door receipt's line fields from its name and the recorded
    rates: the amount as said, and either its dollar value with the rate
    that set it, or ``rate_pending``. Empty for anything else. The one place
    the dollar value of a foreign receipt is computed (the month job and the
    reviewer's month tool both read it)."""
    claim = parse_foreign_claim(name)
    if claim is None:
        return {}
    day, amount, currency = claim
    fields = {"said_amount": f"{amount:.2f}", "said_currency": currency, "fx_date": day}
    rate = rates.get((currency, day))
    if rate is None:
        return {**fields, "amount_cents": None, "rate_pending": True}
    return {
        **fields,
        "amount_cents": rate.cents(amount),
        "fx_rate": rate.per_usd,
        "fx_source": rate.source,
    }
