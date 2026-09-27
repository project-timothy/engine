"""Reconcile decisions: cleared-payment evidence against open ledger rows.

Pure functions, no I/O: the job in ``jobs.py`` feeds them rows and evidence
and acts on the returned decisions. Every rule is deterministic engine code
(invariant 4); the LLM plays no part in payment matching.

Decision vocabulary (one per evidence line):

- ``already_recorded``: a settled row (or settled check group) already
  explains this money — same payee and amount with a compatible payment
  date, or the same check number. Counted, nothing flips, no anomaly. Born
  of the 2026-07-16 live-shadow catch: a vendor paid 50/50 in identical
  halves nearly had June's cleared payment settle July's still-open twin.
  An expense report's own Purchase is the second shape (issue #281): the
  engine wrote that record itself at ``expenses match``, so the report is
  the ledger row for that money and the decision names the report.
- ``settle``: exactly one interpretation exists; flip the named rows to Paid.
- ``review``: candidates exist but more than one interpretation does — or
  the only candidate was committed AFTER the money cleared, which means the
  match is chronologically impossible. Park an approval card, touch nothing.
- ``unknown``: money left the account with no open row to explain it, for a
  payee the ledger knows or by check. Anomaly (2026-05-19 orphan-check class,
  and the double-payment signal when a settled row matches).
- ``ref_backfill``: the bank's check number belongs on a row the ledger
  already settled without one (phase 7 row 7.4). Nothing flips — the row is
  already Paid — the reference lands and the money counts as already
  recorded. Born of a live row that sat Paid with an empty ``check_ref``
  while the statement carried the number all along.
- ``out_of_scope``: cardswipe-style spend with no check number and no ledger
  presence; AP reconciliation is not the expense report. Counted -- and, at or
  above ``[qbo].reconcile_trace_floor_cents``, recorded as
  ``ap.reconcile.out_of_scope`` so real money cannot fall past every rule in
  silence (check 3048, 2026-09-14: a five-figure payment sat in this sink for two months
  because the QBO row carried no payee and no DocNumber).

Two evidence sources feed the same vocabulary. :func:`decide` answers a
cleared transaction the accounting system reports; :func:`decide_statement`
answers a check line from the bank's own statement file (phase 7 row 7.3).
The statement is the only source that can settle a payment the engine
recorded itself (write-side rule 3 keeps the engine's own BillPayment out of
QBO evidence), so it matches on the instrument alone: check reference +
amount + a date window around the recorded payment date, never a payee
(bank text is not a vendor name). One deliberate exception to that rule
lives in :func:`decide_statement`: a check the BANK writes (bill pay) has no
number until it clears, so a committed row on such a channel can settle on
channel + amount + date. The decision is
``docs/decisions/2026-09-17-bill-pay-checks-settle-on-channel.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .registry import VendorRegistry, canonical_vendor
from .status import is_committed, is_settled

Row = dict[str, Any]

# Entry-vs-clearing skew, asymmetric on purpose: a clearing BEFORE the
# recorded payment date beyond a few days of entry skew is suspicious (it
# probably belongs to an earlier obligation), while a clearing well AFTER is
# ordinary life — a mailed check sits in someone's truck for weeks.
DATE_SLACK = timedelta(days=5)
DEPOSIT_LAG = timedelta(days=21)


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _dates_compatible(row_payment_date: Any, evidence_date: Any) -> bool:
    """A settled row explains a clearing when the clearing falls inside
    [payment_date - slack, payment_date + deposit lag], or the row never
    recorded a payment date at all."""
    recorded, cleared = _to_date(row_payment_date), _to_date(evidence_date)
    if recorded is None or cleared is None:
        return True
    return recorded - DATE_SLACK <= cleared <= recorded + DEPOSIT_LAG


def norm_check_ref(ref: str) -> str:
    """Comparable token for a check reference: lowercase alphanumerics with a
    leading "check"/"ck"/"no" prefix stripped. "Check 9051" == "9051"; a messy
    free-text ref simply fails to equal anything and falls through to
    vendor+amount matching, which is the intended degradation."""
    token = re.sub(r"[^a-z0-9]", "", ref.lower())
    return re.sub(r"^(check|chk|ck|no|num)", "", token)


@dataclass
class Decision:
    kind: str  # already_recorded | settle | ref_backfill | review | unknown | out_of_scope
    evidence: Any  # the QboEvidence-shaped mapping this decision answers
    rows: list[Row] = field(default_factory=list)  # rows to settle / candidates
    reason: str = ""
    # The expense report this clearing belongs to, when the accounting record
    # IS one the engine wrote for a report (issue #281). ``rows`` stays the
    # ``ap_invoices`` vocabulary; an expense report never has a row there.
    expense_report_id: int | None = None


def expense_report_for(evidence: Any, purchases: Mapping[str, Row] | None) -> Row | None:
    """The expense report whose recorded Purchase IS this clearing.

    ``purchases`` maps a raw accounting-system Purchase id to the report row
    that carries it (``expense_report.qbo_purchase_id``, written by
    ``expenses match`` the instant the record is created). The match is that
    exact id and nothing else: no payee, no amount, no date window. An
    expense report commits money without an ``ap_invoices`` row, so this is
    the only evidence that the clearing is already recorded.
    """
    if not purchases:
        return None
    entity, _, ident = str(getattr(evidence, "qbo_id", "") or "").partition(":")
    if entity != "Purchase" or not ident:
        return None
    return purchases.get(ident)


@dataclass
class StatementEvidence:
    """One money-out check line from the bank's statement file, normalized
    for matching. ``statement_id`` is the adapter's content identity (date +
    cents + reference); ``amount_cents`` is the positive magnitude."""

    statement_id: str
    date: str  # ISO YYYY-MM-DD, the day the bank cleared it
    amount_cents: int
    check_ref: str
    description: str = ""
    txn_type: str = "Statement"
    payee: str = ""  # a statement carries bank text, never a vendor name


@dataclass
class GroupDecision:
    """A joint answer for several payments that together explain one row
    (issue #115: partial payments against one bill)."""

    kind: str  # settle | review
    evidence: list[Any]  # every payment the decision consumes
    rows: list[Row] = field(default_factory=list)
    reason: str = ""


def _rows_for_payee(rows: list[Row], payee: str, registry: VendorRegistry | None) -> list[Row]:
    token = canonical_vendor(payee, registry)
    return [r for r in rows if canonical_vendor(str(r["vendor"]), registry) == token]


def _cleared_before_commitment(candidates: list[Row], ev_date: Any) -> bool:
    """True when the clearing predates EVERY candidate's commitment beyond
    entry slack — a chronologically impossible match. ``payment_date`` is the
    strongest basis; a real transition into a committed status is the
    fallback (row-birth timestamps are import/test artifacts)."""
    cleared = _to_date(ev_date)
    if cleared is None:
        return False
    impossible = 0
    for row in candidates:
        basis = _to_date(row.get("payment_date")) or _to_date(row.get("scheduled_at"))
        if basis is not None and cleared < basis - DATE_SLACK:
            impossible += 1
    return impossible > 0 and impossible == len(candidates)


def decide(
    evidence: Any,
    rows: list[Row],
    *,
    registry: VendorRegistry | None = None,
    expense_purchases: Mapping[str, Row] | None = None,
) -> Decision:
    """Resolve one cleared transaction against the tenant's ledger rows.

    ``rows`` is EVERY row in the tenant's book, shadow-born included (the
    live book contains rows created by pre-cutover shadow intake). Open rows
    are the match candidates; settled rows explain already-recorded money
    and establish that a payee is known.
    """
    open_rows = [r for r in rows if not is_settled(str(r["status"]))]
    settled_rows = [r for r in rows if is_settled(str(r["status"]))]
    payee = str(getattr(evidence, "payee", "") or "")
    check = norm_check_ref(str(getattr(evidence, "check_ref", "") or ""))
    amount = int(getattr(evidence, "amount_cents", 0))
    ev_date = str(getattr(evidence, "date", "") or "")

    def _chronology_guard(candidates: list[Row]) -> Decision | None:
        """A clearing that predates every candidate's commitment cannot be
        theirs (see :func:`_cleared_before_commitment`)."""
        if _cleared_before_commitment(candidates, ev_date):
            return Decision(
                "review",
                evidence,
                rows=candidates,
                reason=(
                    "cleared before the candidate row(s) were committed; "
                    "this payment likely belongs to an earlier obligation"
                ),
            )
        return None

    # The engine's own expense-report Purchase (issue #281) answers FIRST,
    # ahead of every rule below: the engine wrote this record itself at
    # ``expenses match``, so the money is recorded by construction and the
    # report is its ledger row. Exact id, no heuristic, and identity outranks
    # every heuristic under it. One payee can be both an AP vendor with open
    # rows and an expense-report person, so a same-amount open row would
    # otherwise be settled by the report's own Purchase under step 2, and the
    # reference the report carries ("EXP-2") is a truthy check to step 3, which
    # made every expense report that ever cleared unknown money the owner had
    # to mute by hand (Purchase 275, Purchase 304).
    report = expense_report_for(evidence, expense_purchases)
    if report is not None:
        return Decision(
            "already_recorded",
            evidence,
            reason=(
                f"expense report #{report['id']} was recorded in the accounting "
                "system as this Purchase"
            ),
            expense_report_id=int(report["id"]),
        )

    # 0) The check number is the strongest reference, so it resolves first,
    #    settled before open: a settled group carrying this exact check
    #    already explains the money; failing that, an open row carrying it
    #    settles. Issue #134: the open tier must run BEFORE any payee-level
    #    absorb, or a split payment's settled first half absorbs the second
    #    half's own check and the open twin never settles.
    if check:
        settled_group = [
            r for r in settled_rows if norm_check_ref(str(r["check_ref"] or "")) == check
        ]
        if settled_group and sum(int(r["amount_cents"]) for r in settled_group) == amount:
            if all(_dates_compatible(r["payment_date"], ev_date) for r in settled_group):
                return Decision("already_recorded", evidence, rows=settled_group)
        group = [r for r in open_rows if norm_check_ref(str(r["check_ref"] or "")) == check]
        if group:
            if sum(int(r["amount_cents"]) for r in group) == amount:
                return _chronology_guard(group) or Decision("settle", evidence, rows=group)
            return Decision(
                "review",
                evidence,
                rows=group,
                reason=(
                    f"check {getattr(evidence, 'check_ref', '')} names "
                    f"{len(group)} open row(s) whose sum disagrees with the cleared amount"
                ),
            )

    # 1) A settled row that already explains this money wins over the open
    #    tiers below — the 2026-07-16 near-miss protection: a settled
    #    payment's identical-amount OPEN twin must not absorb a checkless
    #    clearing. Issue #134's boundary: a settled row whose recorded check
    #    CONFLICTS with the evidence's check is a different physical payment
    #    and never absorbs (a settled row with no ref still can — the
    #    intended degradation for checkless channels).
    if payee:
        recorded = [
            r
            for r in _rows_for_payee(settled_rows, payee, registry)
            if int(r["amount_cents"]) == amount
            and _dates_compatible(r["payment_date"], ev_date)
            and not (check and norm_check_ref(str(r["check_ref"] or "")) not in ("", check))
        ]
        if recorded:
            return Decision("already_recorded", evidence, rows=recorded)

    # 2) Payee + exact amount within the open rows.
    if payee:
        candidates = _rows_for_payee(open_rows, payee, registry)
        exact = [r for r in candidates if int(r["amount_cents"]) == amount]
        if len(exact) == 1:
            return _chronology_guard(exact) or Decision("settle", evidence, rows=exact)
        if len(exact) > 1:
            return Decision(
                "review",
                evidence,
                rows=exact,
                reason=f"{len(exact)} open rows for this payee share the cleared amount",
            )

    # 3) Nothing open explains the money. If the payee has ledger presence or
    #    the payment went by check, that is an anomaly, never a guess.
    payee_known = bool(payee) and bool(_rows_for_payee(rows, payee, registry))
    if check or payee_known:
        return Decision(
            "unknown",
            evidence,
            reason="cleared payment with no matching open ledger row",
        )
    return Decision("out_of_scope", evidence)


def group_unexplained(
    evidence_list: list[Any],
    rows: list[Row],
    *,
    registry: VendorRegistry | None = None,
) -> list[GroupDecision]:
    """Joint interpretations for payments :func:`decide` called unknown.

    Issue #115: a bill paid with N physical checks clears as N partial
    BillPayments, none of which equals any row total, so each lands
    ``unknown`` and the row ages forever after the money already moved.

    Two tiers, in strength order:

    - Bill linkage (identity): payments applying to the same QBO bill,
      where exactly one open row carries that ``qbo_bill_id``. Sum equals
      the row -> settle. Sum short, over, or chronologically impossible ->
      one review card naming every payment. A single linked partial also
      parks review — the money IS explained, it just isn't complete yet.
    - Vendor sum (amount-based): with no linkage, two or more payments to
      one vendor summing to exactly ONE open row's amount propose the group
      on a review card, never a settle — amount is a confirmation field,
      never the discriminator (2026-05-21).

    Payments no tier consumes stay with their per-payment ``unknown``.
    """
    decisions: list[GroupDecision] = []
    open_rows = [r for r in rows if not is_settled(str(r["status"]))]
    remaining = list(evidence_list)

    # Tier 1: group by the linked bill (payments applying to exactly one).
    by_bill: dict[str, list[Any]] = {}
    for ev in remaining:
        if str(getattr(ev, "txn_type", "")) != "BillPayment":
            continue
        linked = list(getattr(ev, "linked_bill_ids", []) or [])
        if len(linked) == 1:
            by_bill.setdefault(linked[0], []).append(ev)
    for bill_id, group in sorted(by_bill.items()):
        candidates = [r for r in open_rows if str(r.get("qbo_bill_id") or "") == bill_id]
        if not candidates:
            continue
        total = sum(int(getattr(ev, "amount_cents", 0)) for ev in group)
        earliest = min(str(getattr(ev, "date", "") or "") for ev in group)
        row_sum = sum(int(r["amount_cents"]) for r in candidates)
        if len(candidates) > 1:
            reason = f"{len(candidates)} open rows share this bill; a human picks"
        elif _cleared_before_commitment(candidates, earliest):
            reason = (
                "cleared before the row was committed; these payments "
                "likely belong to an earlier obligation"
            )
        elif total == row_sum:
            decisions.append(GroupDecision("settle", group, rows=candidates))
            for ev in group:
                remaining.remove(ev)
            continue
        else:
            reason = (
                f"{len(group)} payment(s) on this bill total "
                f"${total / 100:,.2f} of the row's ${row_sum / 100:,.2f}"
            )
        decisions.append(GroupDecision("review", group, rows=candidates, reason=reason))
        for ev in group:
            remaining.remove(ev)

    # Tier 2: no linkage — vendor + sum agreement proposes, never settles.
    by_vendor: dict[str, list[Any]] = {}
    for ev in remaining:
        if str(getattr(ev, "txn_type", "")) != "BillPayment":
            continue
        payee = str(getattr(ev, "payee", "") or "")
        if payee:
            by_vendor.setdefault(canonical_vendor(payee, registry), []).append(ev)
    for _token, group in sorted(by_vendor.items()):
        if len(group) < 2:
            continue
        payee = str(getattr(group[0], "payee", ""))
        total = sum(int(getattr(ev, "amount_cents", 0)) for ev in group)
        matches = [
            r
            for r in _rows_for_payee(open_rows, payee, registry)
            if int(r["amount_cents"]) == total
        ]
        if len(matches) != 1:
            continue  # ambiguous or absent: the per-payment unknowns stand
        decisions.append(
            GroupDecision(
                "review",
                group,
                rows=matches,
                reason=(
                    f"{len(group)} payments to this vendor sum to the open "
                    "row's amount; no bill linkage, so a human confirms"
                ),
            )
        )
    return decisions


# ---- the statement tier (phase 7 row 7.3) -----------------------------------


def statement_evidence(lines: list[Any]) -> list[StatementEvidence]:
    """The statement lines the tier can answer: money out (negative, the
    bank's own sign) carrying a check number. Deposits, ACH, and card lines
    carry bank text and no instrument the ledger records; the bank rules and
    the expenses lane own those, so they are never unknown money here."""
    from ...adapters.bank_csv import amount_cents, line_identity

    out: list[StatementEvidence] = []
    for line in lines:
        cents = amount_cents(line)
        ref = str(line.check_ref or "").strip()
        if cents >= 0 or not ref:
            continue
        out.append(
            StatementEvidence(
                statement_id=line_identity(line),
                date=str(line.date),
                amount_cents=-cents,
                check_ref=ref,
                description=str(line.description or ""),
            )
        )
    return out


def _candidate_groups(members: list[Row], registry: VendorRegistry | None) -> list[list[Row]]:
    """The interpretations of "the open rows under this check reference":
    each engine-recorded payment (rows sharing a ``qbo_payment_id``) is one,
    rows with no payment record group by vendor, and when more than one
    exists the whole set is a candidate too (a check whose scope moved after
    its payment was recorded still clears as one amount)."""
    by_payment: dict[str, list[Row]] = {}
    loose: dict[str, list[Row]] = {}
    for row in members:
        payment_id = str(row.get("qbo_payment_id") or "")
        if payment_id:
            by_payment.setdefault(payment_id, []).append(row)
        else:
            loose.setdefault(canonical_vendor(str(row["vendor"]), registry), []).append(row)
    groups = [g for _, g in sorted(by_payment.items())] + [g for _, g in sorted(loose.items())]
    if len(groups) > 1:
        groups.append(list(members))
    return groups


def digit_tokens(ref: str) -> set[str]:
    """Every run of digits inside a reference. A human-typed ``check_ref``
    names its instrument in free text — "Checks 9032 + 9033", "Check 9025
    (cleared 4/3)", "5/3 bill pay 9053" — and the number is in there
    somewhere; this is how it is found without pretending the whole string
    is a reference."""
    return {t for t in re.split(r"\D+", str(ref or "")) if t}


# The shortest digit run that can be a check number. Free text is full of
# short numbers that are not references — "5/3 bill pay 9053" carries a bank
# name and a date beside its one check — and a two-digit fragment counting as
# a reference would let a date silence a real check line.
MIN_CHECK_DIGITS = 4


def names_check(ref: str, check: str) -> bool:
    """Does this row's reference name that check number?

    Whole-token containment, four digits or more: a number embedded in a
    larger number is not a match, so "19032" never answers check 9032, and a
    short fragment is not a reference at all.
    """
    if len(check) < MIN_CHECK_DIGITS:
        return False
    return check in {t for t in digit_tokens(ref) if len(t) >= MIN_CHECK_DIGITS}


def _numberless(ref: str) -> bool:
    """A reference carrying no digit at all is not a reference to an
    instrument: "Electronic", "ACH", "Zelle" say how the money moved, not
    which check it was. For the backfill it counts as empty."""
    return not digit_tokens(ref)


def _reference_backfill_groups(settled_rows: list[Row], line: StatementEvidence) -> list[list[Row]]:
    """Interpretations of "the settled money this check paid, on a row that
    never got the number": settled rows carrying NO number, whose date fits
    the clearing, grouped by engine payment record (rows sharing a
    ``qbo_payment_id``) or standing alone, that sum to the cleared amount.

    A settled row carrying a DIFFERENT number is a different physical
    payment and is never a candidate (#134's boundary). A row whose
    reference is a channel word rather than a number IS a candidate: it has
    no instrument recorded, which is the state this tier repairs.
    """
    pool = [
        r
        for r in settled_rows
        if _numberless(str(r["check_ref"] or ""))
        and _dates_compatible(r["payment_date"], line.date)
    ]
    groups: list[list[Row]] = []
    by_payment: dict[str, list[Row]] = {}
    for row in pool:
        payment_id = str(row.get("qbo_payment_id") or "")
        if payment_id:
            by_payment.setdefault(payment_id, []).append(row)
        else:
            groups.append([row])
    groups.extend(g for _, g in sorted(by_payment.items()))
    return [g for g in groups if sum(int(r["amount_cents"]) for r in g) == int(line.amount_cents)]


def _bill_pay_candidates(
    open_rows: list[Row],
    line: StatementEvidence,
    registry: VendorRegistry | None,
    channels: Sequence[str],
) -> list[Row]:
    """Committed rows a bank-written check could be (issue #285).

    THE DELIBERATE EXCEPTION to 7.3's rule that the check number is the
    discriminator. When the bank writes the check, the tenant never has a
    number to record: the row is committed with a send date and an empty
    reference, and the statement line that finally carries the number can
    match nothing. Four conditions, all required, and the caller settles
    only on exactly one candidate:

    - the vendor's registry ``payment_channel`` is one the tenant named as
      bank-written (``[qbo].bill_pay_channels``). A vendor the tenant pays
      by hand-written check keeps the old rule, where the number it wrote is
      the discriminator;
    - the row is committed money (Scheduled), never payable and never
      settled;
    - the row carries NO instrument reference. A row naming a different
      number is a different physical payment and is never a candidate
      (the #134 boundary);
    - the clearing fits the tier's usual window around the recorded date
      (:func:`_dates_compatible`: up to ``DEPOSIT_LAG`` after it, at most
      ``DATE_SLACK`` before). On a bill-pay row that recorded date is the
      day the BANK SENDS the check, and the clearing is the day the payee
      deposits it, one to three weeks later, so the lag is the whole point;
      a clearing before the send date beyond entry slack is not this check.
      A row with no recorded date is not a candidate at all: the helper
      passes a missing date, which is right when a reference already named
      the row and wrong here, where the date is one of three discriminators.
    """
    if not channels or registry is None:
        return []
    wanted = {c.strip().lower() for c in channels if c.strip()}
    if not wanted or _to_date(line.date) is None:
        return []
    candidates: list[Row] = []
    for row in open_rows:
        if not is_committed(str(row["status"])):
            continue
        if not _numberless(str(row["check_ref"] or "")):
            continue
        if int(row["amount_cents"]) != int(line.amount_cents):
            continue
        if _to_date(row.get("payment_date")) is None:
            continue
        if not _dates_compatible(row.get("payment_date"), line.date):
            continue
        entry = registry.resolve_ledger_name(str(row["vendor"]))
        if entry is None or entry.payment_channel.strip().lower() not in wanted:
            continue
        candidates.append(row)
    return candidates


def decide_statement(
    line: StatementEvidence,
    rows: list[Row],
    *,
    registry: VendorRegistry | None = None,
    bill_pay_channels: Sequence[str] = (),
) -> Decision:
    """Resolve one statement check line against the tenant's ledger rows.

    Same vocabulary as :func:`decide`, instrument-only: the check reference
    names the rows, the amount must equal a candidate group's sum, and the
    clearing must fall inside the entry-slack / deposit-lag window around
    the group's recorded payment date. Exactly one fitting group settles;
    several park review (a reused number); candidates that fit on reference
    but not on amount or date park review too (a human decides whether the
    number was reused for an earlier obligation).

    With no open row under the reference at all, one last tier runs before
    the money is called unknown: a SETTLED row carrying no reference whose
    amount and date fit takes the number (``ref_backfill``). That tier is
    reference-restoring, never settling — **amount and date never settle an
    open row**, because the check number is the discriminator for settling
    and amount is a confirmation field (2026-05-21). Several fitting settled
    rows park review.

    ``bill_pay_channels`` names the one exception to that rule (issue #285,
    see :func:`_bill_pay_candidates`): a check the BANK writes has no number
    until it clears, so a committed row on such a channel settles on channel
    + amount + the send-to-deposit window when it is the ONLY one. Two candidates park review; the
    tier is inert for a tenant that names no channel. Nothing left is
    unknown money, the orphan-check class.
    """
    check = norm_check_ref(line.check_ref)
    if not check:
        return Decision("out_of_scope", line)
    amount = int(line.amount_cents)
    open_rows = [r for r in rows if not is_settled(str(r["status"]))]
    settled_rows = [r for r in rows if is_settled(str(r["status"]))]

    # 1) The same reference on settled rows summing to the cleared amount is
    #    the same physical check, and the date window has no work to do:
    #    check numbers do not repeat on an account, while legacy rows carry
    #    import-artifact payment dates (an invoice date, a Finder tag). The
    #    #134 boundary is untouched — a CONFLICTING reference never absorbs.
    settled_group = [r for r in settled_rows if norm_check_ref(str(r["check_ref"] or "")) == check]
    if settled_group and sum(int(r["amount_cents"]) for r in settled_group) == amount:
        return Decision("already_recorded", line, rows=settled_group)

    members = [r for r in open_rows if norm_check_ref(str(r["check_ref"] or "")) == check]
    if not members:
        # 2) The legacy free text a human typed into check_ref: one row paid
        #    with two checks names both ("Checks 9032 + 9033"), a row names
        #    its check beside a note ("Check 9025 (cleared 4/3)", "5/3 bill
        #    pay 9053"). Containment is the whole test — no amount, no date
        #    — because the only outcome is "do not flag this as unknown
        #    money", and a row paid with two checks answers neither line by
        #    its own amount. A reference typed wrong is the accepted risk:
        #    the alternative is asking the owner about money his own book
        #    already explains, every night, until he mutes the tier.
        named = [r for r in settled_rows if names_check(str(r["check_ref"] or ""), check)]
        if named:
            return Decision(
                "already_recorded",
                line,
                rows=named,
                reason="legacy reference names the check",
            )
        backfill = _reference_backfill_groups(settled_rows, line)
        if len(backfill) == 1:
            return Decision("ref_backfill", line, rows=backfill[0])
        if len(backfill) > 1:
            return Decision(
                "review",
                line,
                rows=[r for g in backfill for r in g],
                reason=(
                    f"check {line.check_ref} fits {len(backfill)} settled row(s) that "
                    "carry no reference; a human names the row"
                ),
            )
        # 3) The bill-pay exception (#285): the bank wrote this check, so no
        #    number could have been recorded at scheduling. Channel + amount
        #    + a tight date window, and only when exactly one committed row
        #    fits; two rows at one amount in one week is a review, never a
        #    guess. Runs AFTER the backfill tier above on purpose: money the
        #    book already recorded as paid explains the line first, and a
        #    committed row that also fits keeps waiting for its own clearing.
        bill_pay = _bill_pay_candidates(open_rows, line, registry, bill_pay_channels)
        if len(bill_pay) == 1:
            return Decision(
                "settle",
                line,
                rows=bill_pay,
                reason="bank-written bill-pay check: one committed row fits channel, amount, date",
            )
        if len(bill_pay) > 1:
            return Decision(
                "review",
                line,
                rows=bill_pay,
                reason=(
                    f"check {line.check_ref} fits {len(bill_pay)} committed bill-pay rows "
                    "at this amount and date; the bank wrote the number, so nothing "
                    "distinguishes them but a human"
                ),
            )
        return Decision(
            "unknown",
            line,
            reason="cleared check with no matching open ledger row",
        )
    groups = _candidate_groups(members, registry)
    fitting = [
        g
        for g in groups
        if sum(int(r["amount_cents"]) for r in g) == amount
        and all(_dates_compatible(r["payment_date"], line.date) for r in g)
    ]
    if len(fitting) == 1:
        return Decision("settle", line, rows=fitting[0])
    if len(fitting) > 1:
        seen: set[int] = set()
        union = [r for g in fitting for r in g if not (r["id"] in seen or seen.add(r["id"]))]
        return Decision(
            "review",
            line,
            rows=union,
            reason=(
                f"check {line.check_ref} names {len(fitting)} candidate payment groups "
                "that each equal the cleared amount; a human picks"
            ),
        )
    if any(sum(int(r["amount_cents"]) for r in g) == amount for g in groups):
        reason = (
            f"check {line.check_ref} cleared outside the window around the recorded "
            "payment date; this may belong to an earlier obligation reusing the number"
        )
    else:
        reason = (
            f"check {line.check_ref} names {len(members)} open row(s) whose sum "
            "disagrees with the cleared amount"
        )
    return Decision("review", line, rows=members, reason=reason)
