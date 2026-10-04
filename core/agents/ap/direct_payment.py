"""The hand-check lane: money that left the bank with no ledger row to explain it.

Phase 7 row 7.4 (issue #213), the second card producer, specified in #257.

The premise, in the owner's words on 2026-09-14: *"I'm not going to change
how a 68-year-old man pays for things."* One owner writes hand checks to
contractors and that is permanent, normal operation, not a deviation. So a
lens that WARNs every time one happens would be a nightly nag about the
company working correctly; it would be muted inside a month and the escapes
would resume. This is a first-class PATH instead: the engine reads the
clearing, proposes the payable, and parks a card.

Why it exists at all. A 1099 needs four things maintained by hand in four
places (the W-9 paper, the ``vendors.toml`` row, a QBO payment attributed to
a payee, and a ledger row); missing any one drops the person. The two
detectors that should catch that both start downstream of a ledger row:
lens 12 starts FROM the registry, and the CPA appendix builds taxpayer units
from ``ap_invoices`` plus ``expense_report``. A contractor paid direct from
checking has no ledger row, so he is invisible to both. One live case: a
contractor who had been a filed 1099 recipient the PRIOR tax year carried no
registry row for nine months of the next one, and was found only by accident
because an unrelated large check prompted a question.

**The discriminator is the cost type, not the account.** A freight vendor
(excluded from 1099-NEC) and a subcontractor (reportable) can code to the
SAME project-expense GL account, and in at least one live chart they do.
Only the registry's ``cost_type`` separates them, so:

- a payee the registry knows cards on its cost type alone, and a
  non-1099 cost type (``Freight/Shipping``, ``Materials``, ``Insurance``)
  never cards at any amount;
- a payee the registry does NOT know has no cost type to read, so the
  account is the only signal left, and a 1099-shaped account pattern
  cards. This covers both the unregistered contractor and the payee-less
  payment: an accounting system's bank feed routinely supplies neither a
  payee nor a document number, and a payment attributed to nobody appears
  on nobody's 1099.

**The bank statement is the second source** (2026-09-16). A statement check
line carries no payee and no coding at all, so neither discriminator above
can run — and what it does carry is stronger for this lane's purpose: a
check cleared and nothing in the book explains it. The bank never loses the
check number; the accounting feed routinely supplies neither a payee nor a
document number, which is exactly how a contractor's payments come to count
toward nobody's 1099.

Pure functions, no I/O: ``jobs.py`` feeds these a decision and acts on the
proposal. Nothing here decides anything a model could; the rules are
deterministic engine code (invariant 4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date as _date
from typing import Any

from .reconcile import DATE_SLACK, norm_check_ref
from .registry import VendorRegistry

# Cost types that describe work a person performs, which is what 1099-NEC
# reports. Tenant-overridable, because a tenant's chart is its own.
DEFAULT_COST_TYPES: tuple[str, ...] = ("subcontractors", "freelancers", "reimbursement")

# Account-name fragments that read as contractor work when there is no
# registry row to ask. Deliberately narrow: this fires only for payees the
# registry cannot classify, so a false positive here costs the owner a card
# to reject, while a false negative costs a missing 1099.
DEFAULT_ACCOUNT_PATTERNS: tuple[str, ...] = (
    "project expense",
    "subcontractor",
    "contract labor",
    "professional fees",
)

# Only the decisions that mean "money left and nothing in the book explains
# it". ``already_recorded`` and ``settle`` are explained by definition, and
# ``review`` already has its own card naming candidate rows.
CARDABLE_DECISIONS: frozenset[str] = frozenset({"out_of_scope", "unknown"})

# A P-number as the house chart spells it inside an account name. This is a
# display hint for the card, NOT a second canonicalizer: the canonical
# implementation is ``auditor/pn.py``, which core deliberately does not
# import (the package-independence lint keeps core free of auditor).
_PN_IN_ACCOUNT = re.compile(r"PN?\s?(\d{2})\s?_?\s?(\d{4})", re.IGNORECASE)


@dataclass(frozen=True)
class Proposal:
    """A cleared payment the engine proposes to record as a payable."""

    qbo_id: str
    payee: str  # "" when the feed named nobody; the owner supplies it
    amount_cents: int
    date: str
    accounts: tuple[str, ...]
    cost_type: str  # the registry's, or "" when the payee is unregistered
    project_hint: str  # read off the account name; "" when unreadable
    basis: str  # why this carded, in words, for the card's reason line

    @property
    def needs_payee(self) -> bool:
        """The feed named nobody, so the owner must before a row can exist."""
        return not self.payee


# ---- the second cross-source identity (#305) --------------------------------
#
# One physical check reaches the owner from two directions: the bank
# statement, which never loses the check number, and the accounting feed,
# which routinely carries neither a payee nor a document number. The lane's
# first identity is (check number, amount), and that one cannot see a pair
# where only ONE side has the number: on the lane's first live day a single
# hand check parked twice, and approving both cards would have created two
# payable rows for one payment.
#
# So there is a second identity, for exactly that case. It is deliberately
# NOT "same amount": amount alone is never the discriminator in this lane
# (two contractors on the same rate card clear identical checks all month).
# It is a narrower test, all three clauses required:
#
#   1. the same cents, AND
#   2. the same cleared date, exactly or inside the reconcile date slack
#      (a bank posts on a day the accounting record need not), AND
#   3. the numbered side's number appears on NO other feed evidence in the
#      window, so nothing else could be the accounting record for it.
#
# And it answers only when exactly one candidate fits, from both directions.
# Two candidates is an unanswered question, not a coin toss (invariant 2).

CROSS_SOURCE_SLACK_DAYS: int = DATE_SLACK.days


@dataclass(frozen=True)
class Payment:
    """One side's view of a cleared payment, reduced to the four facts the
    cross-source identity reads. Built from a statement line, a feed record,
    or a parked card alike, so the rule below never touches a live object."""

    ident: str
    amount_cents: int
    date: str
    check_ref: str = ""


def _day(value: Any) -> _date | None:
    try:
        return _date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def cross_source_twin(
    numbered: Payment,
    numberless: Sequence[Payment],
    *,
    feed_numbers: Iterable[str] = (),
    slack_days: int = CROSS_SOURCE_SLACK_DAYS,
) -> Payment | None:
    """The one numberless payment that IS this numbered one, or None.

    ``numbered`` is the side carrying the check number (a bank statement
    line, or the card parked from one); ``numberless`` is the side that
    carries none. ``feed_numbers`` is every check number the accounting feed
    shows in the window: the number appearing there at all means some other
    record is the accounting side of this check, so nothing here pairs.

    Amount alone is never the discriminator. This is amount AND date AND the
    absence of competing evidence, and it returns None the moment two
    candidates fit rather than choosing between them.
    """
    number = norm_check_ref(numbered.check_ref)
    if not number:
        return None
    if number in {norm_check_ref(str(n)) for n in feed_numbers if str(n).strip()}:
        return None
    anchor = _day(numbered.date)
    if anchor is None:
        return None
    # Keyed by identity, because the same payment reaches this rule from
    # more than one place in a run (the card a previous run parked and the
    # evidence that parked it are one payment, not two candidates).
    hits: dict[str, Payment] = {}
    for candidate in numberless:
        if norm_check_ref(candidate.check_ref):
            # Both sides have a number: the (number, amount) identity owns
            # that pair, and it is the stronger one.
            continue
        if candidate.amount_cents != numbered.amount_cents:
            continue
        cleared = _day(candidate.date)
        if cleared is None or abs((cleared - anchor).days) > slack_days:
            continue
        hits.setdefault(candidate.ident, candidate)
    return next(iter(hits.values())) if len(hits) == 1 else None


def cross_source_pairs(
    numbered: Sequence[Payment],
    numberless: Sequence[Payment],
    *,
    feed_numbers: Iterable[str] = (),
    slack_days: int = CROSS_SOURCE_SLACK_DAYS,
) -> dict[str, str]:
    """``{numbered ident: numberless ident}`` for every pair that is unique
    from BOTH sides.

    Uniqueness one way is not enough: two checks written the same day for
    the same amount cannot both be the one numberless payment, and pairing
    either of them would be the guess this lane refuses.
    """
    claims: dict[str, str] = {}
    for one in numbered:
        twin = cross_source_twin(one, numberless, feed_numbers=feed_numbers, slack_days=slack_days)
        if twin is not None:
            claims[one.ident] = twin.ident
    contested = {ident for ident in claims.values() if list(claims.values()).count(ident) > 1}
    return {k: v for k, v in claims.items() if v not in contested}


def pn_hint(accounts: tuple[str, ...] | list[str]) -> str:
    """The first P-number spelled inside an account name, canonical form."""
    for account in accounts:
        found = _PN_IN_ACCOUNT.search(str(account))
        if found:
            return f"P{found.group(1)}_{found.group(2)}"
    return ""


def _registry_entry(payee: str, registry: VendorRegistry | None) -> Any | None:
    """The registry row for a payee, or None when it knows no such vendor.

    ``resolve_ledger_name`` is the shared lookup: canonical name or a listed
    ``ledger_alias``, exact and case-insensitive, so two genuinely different
    vendors never merge. Using it rather than a local matcher is deliberate:
    a vendor whose registry name and everyday spelling differ by one word had
    every one of his clearings fall into the out-of-scope sink unnoticed
    (2026-09-14), because a local list carried only the formal spelling.
    """
    if not payee or registry is None:
        return None
    return registry.resolve_ledger_name(payee)


def propose(
    evidence: Any,
    decision_kind: str,
    *,
    registry: VendorRegistry | None = None,
    cost_types: tuple[str, ...] = DEFAULT_COST_TYPES,
    account_patterns: tuple[str, ...] = DEFAULT_ACCOUNT_PATTERNS,
    floor_cents: int = 60_000,
) -> Proposal | None:
    """Should this unexplained clearing become a payable proposal?

    Returns the proposal, or None when the payment is none of this lane's
    business. Callers must already have excluded ``reconcile_ignore_payees``
    (owners, bank fees, payroll processors, SaaS): that list is a curated
    decision, and re-litigating it here would give two places to change.
    """
    if decision_kind not in CARDABLE_DECISIONS:
        return None

    amount = int(getattr(evidence, "amount_cents", 0))
    # The floor keeps day one from dumping every small payee-less Travel and
    # Meals charge into the queue. It is a first-pass throttle, not a tax
    # rule: the 1099-NEC obligation aggregates across a year, so a tenant
    # that wants every candidate sets it to 0 and takes the volume.
    if amount < floor_cents:
        return None

    payee = str(getattr(evidence, "payee", "") or "")
    accounts = tuple(str(a) for a in (getattr(evidence, "accounts", ()) or ()))
    check_ref = str(getattr(evidence, "check_ref", "") or "").strip()
    ident = str(getattr(evidence, "qbo_id", "") or getattr(evidence, "statement_id", ""))

    # The bank statement, the second source (phase 7 row 7.4, 2026-09-16). A
    # statement line carries no payee and no coding at all — the bank knows
    # the instrument, not the accounting — so neither discriminator above
    # can run. What IS known is stronger than either for this lane's
    # purpose: a check cleared and nothing in the book explains it. The bank
    # never loses the check number, while the accounting feed routinely
    # supplies neither a payee nor a document number, and that is exactly
    # how a contractor's payments count toward nobody's 1099.
    if str(getattr(evidence, "txn_type", "")) == "Statement":
        if not check_ref:
            return None
        return Proposal(
            qbo_id=ident,
            payee="",
            amount_cents=amount,
            date=str(getattr(evidence, "date", "") or ""),
            accounts=(),
            cost_type="",
            project_hint="",
            basis=f"bank statement check {check_ref}, no payee or coding on a statement line",
        )

    entry = _registry_entry(payee, registry)

    if entry is not None:
        cost_type = str(getattr(entry, "cost_type", "") or "")
        if cost_type.strip().lower() not in {c.strip().lower() for c in cost_types}:
            # The freight case: transportation payments are excluded from
            # 1099-NEC reporting, and freight can code to the same account
            # as subcontract labor. The registry already answered this.
            return None
        basis = f"registry cost type {cost_type!r}"
    else:
        matched = [
            a
            for a in accounts
            if any(p.strip().lower() in a.lower() for p in account_patterns if p.strip())
        ]
        if not matched:
            # The Purchase 223 case: an owner loan payoff codes to
            # Short Term Loans Payable, which is not contractor work.
            return None
        cost_type = ""
        who = "an unregistered payee" if payee else "no payee at all"
        basis = f"{who} and 1099-shaped coding ({matched[0]})"

    return Proposal(
        qbo_id=ident,
        payee=payee,
        amount_cents=amount,
        date=str(getattr(evidence, "date", "") or ""),
        accounts=accounts,
        cost_type=cost_type,
        project_hint=pn_hint(accounts),
        basis=basis,
    )
