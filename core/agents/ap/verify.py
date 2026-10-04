"""Three-way payment verification. Engine code, never agent judgment.

Invariant 4, written in the ~$38K near-miss of 2026-05-21: an item is payable
only if it is open in the AP ledger, absent from the cleared bank register,
AND absent from the bill-pay scheduled/in-process queue. A hit in either of
the latter two means the money is already paid or already committed.

Matching is on identity, never amount alone: payee plus invoice/check
reference plus ledger status. Amounts collide (a single vendor frequently
bills the same figure twice), so amount is a confirmation field. Where only
a payee+amount join is possible, it must resolve to exactly ONE candidate
row; an ambiguous join never silently matches.

The ``state`` dict shape (also the seed-fixture shape):

    {"ap_ledger":    [{"row", "payee", "invoice_ref", "amount", "status",
                       ("check_ref")}],
     "cleared_bank": [{"check_ref", "payee"?, "amount"}],
     "billpay_queue":[{"payee", "invoice_ref"?, "amount", "state"}]}

Real runs build this state from the engine's ap_invoices table, the bank-CSV
adapter, and a bill-pay queue snapshot; the shape stays the same.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .registry import VendorRegistry, canonical_vendor
from .status import is_committed, is_settled

DEFAULT_MATCH_KEYS = ["payee", "invoice_or_check_ref", "ledger_status"]

Row = dict[str, Any]


class AmbiguousMatch(LookupError):
    """A join resolved to more than one ledger row. Never match silently."""


class NoMatch(LookupError):
    """A join resolved to no ledger row."""


@dataclass(frozen=True)
class MatchedRow:
    """A resolved ledger row with attribute access to its fields."""

    data: Row

    def __getattr__(self, name: str) -> Any:
        try:
            return self.data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _digits(ref: Any) -> str:
    """Loose check-number normalization: 'Check 9034', 'CK1039', 4042 -> digits."""
    return "".join(re.findall(r"\d+", str(ref or "")))


def _refs_equal(a: Any, b: Any) -> bool:
    da, db = _digits(a), _digits(b)
    if da and db:
        return da == db
    return str(a or "").strip().lower() == str(b or "").strip().lower() != ""


def _payee_rows(state: Row, payee: str, registry: VendorRegistry | None = None) -> list[Row]:
    """Ledger rows whose payee is the same vendor as ``payee``.

    Compared on the canonical vendor token, not the raw string, so a legacy or
    alternate spelling in a bill-pay/bank snapshot still resolves to the engine's
    row (invariant 4: a committed payment under a different spelling must not read
    as still-payable). With no registry this is an exact case-insensitive match,
    the prior behavior.
    """
    target = canonical_vendor(str(payee), registry)
    return [
        r
        for r in state.get("ap_ledger", [])
        if canonical_vendor(str(r.get("payee", "")), registry) == target
    ]


def _match_by_evidence(
    state: Row, evidence: Row, registry: VendorRegistry | None = None
) -> Row | None:
    """Resolve one external evidence line (bank or queue) to a ledger row.

    Reference first (invoice_ref or check_ref), then unique-candidate payee
    with amount as confirmation. Ambiguity returns None: the caller flags, the
    engine never guesses.
    """
    candidates = state.get("ap_ledger", [])
    payee = evidence.get("payee")
    if payee:
        candidates = _payee_rows(state, str(payee), registry)

    ev_inv = evidence.get("invoice_ref")
    if ev_inv:
        exact = [r for r in candidates if _refs_equal(r.get("invoice_ref"), ev_inv)]
        return exact[0] if len(exact) == 1 else None

    ev_chk = evidence.get("check_ref")
    if ev_chk:
        by_check = [r for r in candidates if _refs_equal(r.get("check_ref"), ev_chk)]
        if len(by_check) == 1:
            return by_check[0]
        if len(by_check) > 1:
            return None

    # No usable reference: a payee join is acceptable only when it is unique,
    # with amount agreement as confirmation (never as the discriminator).
    if not payee or len(candidates) != 1:
        return None
    row = candidates[0]
    ev_amt, row_amt = _dec(evidence.get("amount")), _dec(row.get("amount"))
    if ev_amt is not None and row_amt is not None and ev_amt != row_amt:
        return None
    return row


def classify_payable(
    state: Row,
    *,
    match_keys: list[str] | None = None,
    registry: VendorRegistry | None = None,
) -> dict[tuple[str, str], str]:
    """Classify every ledger row: ``payable`` | ``committed`` | ``paid``.

    Verdicts are keyed on ``(payee, invoice_ref)``.

    The three sources each hold part of the truth. A bill-pay-queue hit is
    committed money; a cleared-bank hit is paid; only a row open in the ledger
    and absent from both is payable. When a vendor registry is supplied, payee
    matching canonicalizes spellings through it, so a committed payment recorded
    under an alias is not missed (invariant 4).
    """
    _validate_match_keys(match_keys)
    queued_ids = set()
    for entry in state.get("billpay_queue", []):
        row = _match_by_evidence(state, entry, registry)
        if row is not None:
            queued_ids.add(id(row))
    cleared_ids = set()
    for line in state.get("cleared_bank", []):
        row = _match_by_evidence(state, line, registry)
        if row is not None:
            cleared_ids.add(id(row))

    verdict: dict[tuple[str, str], str] = {}
    for row in state.get("ap_ledger", []):
        # Keyed on (payee, invoice_ref): invoice numbers are vendor-scoped
        # identifiers, and two vendors both issuing "1001" is ordinary. A
        # ref-only key let one row's verdict silently overwrite its
        # cross-vendor twin (2026-08-20 assessment).
        key = (str(row.get("payee", "")), str(row.get("invoice_ref")))
        status = str(row.get("status", ""))
        if id(row) in queued_ids or is_committed(status):
            verdict[key] = "committed"
        elif id(row) in cleared_ids or is_settled(status):
            verdict[key] = "paid"
        else:
            verdict[key] = "payable"
    return verdict


def payable_refs(
    state: Row,
    *,
    match_keys: list[str] | None = None,
    registry: VendorRegistry | None = None,
) -> list[tuple[str, str]]:
    """The (payee, ref) pairs a payment run may consider, and nothing else."""
    verdict = classify_payable(state, match_keys=match_keys, registry=registry)
    return [key for key, kind in verdict.items() if kind == "payable"]


def match_payment(
    state: Row,
    *,
    payee: str,
    invoice_ref: str | None = None,
    amount: float | Decimal | None = None,
    registry: VendorRegistry | None = None,
) -> MatchedRow:
    """Resolve a payment to exactly one ledger row, or refuse.

    Payee + invoice reference resolves. Payee + amount alone raises
    :class:`AmbiguousMatch` whenever more than one row carries that amount;
    round numbers collide and one vendor bills the same figure twice.
    """
    candidates = _payee_rows(state, payee, registry)
    if invoice_ref is not None:
        exact = [r for r in candidates if _refs_equal(r.get("invoice_ref"), invoice_ref)]
        if len(exact) == 1:
            return MatchedRow(exact[0])
        if len(exact) > 1:
            raise AmbiguousMatch(f"{payee!r} + ref {invoice_ref!r} matches {len(exact)} rows")
        raise NoMatch(f"no row for {payee!r} + ref {invoice_ref!r}")

    if amount is not None:
        amt = _dec(amount)
        by_amount = [r for r in candidates if _dec(r.get("amount")) == amt]
        if len(by_amount) > 1:
            raise AmbiguousMatch(
                f"{payee!r} + amount alone matches {len(by_amount)} rows; "
                "amount is a confirmation field, never the discriminator"
            )
        if len(by_amount) == 1:
            return MatchedRow(by_amount[0])
        raise NoMatch(f"no row for {payee!r} with that amount")

    if len(candidates) == 1:
        return MatchedRow(candidates[0])
    if len(candidates) > 1:
        raise AmbiguousMatch(f"{payee!r} alone matches {len(candidates)} rows")
    raise NoMatch(f"no row for payee {payee!r}")


def _validate_match_keys(match_keys: list[str] | None) -> None:
    if match_keys is None:
        return
    if "payee" not in match_keys or not any("ref" in k for k in match_keys):
        raise ValueError(
            "match keys must include payee and an invoice/check reference; "
            "amount alone is never a match key (2026-05-21)"
        )
