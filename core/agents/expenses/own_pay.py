"""Receipts a person paid from their own pay (board chunk c34, gap C).

Said so on the receipt (``set_category`` with whose "pay"; the word rides
``expense.category_set``), a receipt rides no card and is no one's
spending. Its dollars are a cost on the person's Schedule C, beside the
bank lines they marked the same way. This reads them from the log alone,
priced the way the card would have priced them: an owner's price, else the
person's own words at the receipt's day's rate (recorded when the month
ran), else the dollars they said or the photo read. Nothing is guessed: a
receipt in another currency whose day's rate isn't recorded yet waits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from . import fx, records, statements
from .report_lines import amounts_said, claim_name
from .schema import parse_amount_tag


def of_year(log: list[dict], year: str, whose: Callable[[str], bool]) -> tuple[list[int], int]:
    """(each priced receipt's cents, how many wait on a rate) for the
    person's receipts in ``year`` paid from their own pay."""
    paid = records.from_pay(log)
    if not paid:
        return [], 0
    said = amounts_said(log)
    dated = records.days(log)
    rates = fx.stored(log)
    drawn, _on_hand = statements.cash(log)
    owner = _latest(log, records.PRICED_EVENT)
    read = _latest(log, records.PROPOSED_EVENT)
    cents: list[int] = []
    waiting = 0
    for sha, landed in _landed(log, paid):
        if not whose(str(landed.get("person", ""))):
            continue
        name = records.dated_name(str(landed.get("file", "")), dated.get(sha, ""))
        if name[:4] != year:
            continue
        if sha in said:
            name = claim_name(name, said[sha])
        got = _cents(name, owner.get(sha), read.get(sha), rates, drawn.get(sha))
        if got is None:
            waiting += 1
        else:
            cents.append(got)
    return cents, waiting


def _cents(
    name: str, owner: dict | None, read: dict | None, rates: dict, cash: fx.Drawn | None
) -> int | None:
    if owner and owner.get("amount_cents") is not None:
        return int(owner["amount_cents"])
    fields = fx.priced(name, rates, cash)
    if fields:
        return None if fields.get("rate_pending") else int(fields["amount_cents"])
    if read and read.get("amount_cents") is not None:
        return int(read["amount_cents"])
    return parse_amount_tag(name)


def _latest(log: Iterable[dict], kind: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in log:
        payload = e.get("payload", {})
        if e.get("event_type") == kind and payload.get("sha256"):
            out[str(payload["sha256"])] = payload
    return out


def _landed(log: Iterable[dict], shas: set[str]) -> list[tuple[str, dict]]:
    return sorted(
        _latest(
            (e for e in log if e.get("payload", {}).get("sha256") in shas), records.LANDED_EVENT
        ).items()
    )
