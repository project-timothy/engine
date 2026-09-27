"""ap/reconcile: cleared QBO payments settle Scheduled rows, deterministically.

Closes the loop the owner described on 2026-07-16: "we'll know in QuickBooks
when it clears." The engine reads cleared transactions from the accounting
system and flips committed rows to Paid; a human never re-keys a clearing.

The decision rules under test are engine code, not judgment (invariant 4):

- one clean match -> flip, with payment date and check ref recorded;
- a check covering several rows settles the group only when the sum agrees;
- ambiguity parks an approval card and flips nothing;
- a cleared payment with no open row is an ANOMALY, never a new row and
  never a guess (the 2026-05-19 orphan-check and 2026-05-21 ~$38K classes);
- a payable-status row that clears anyway settles but flags the status lag;
- shadow mode flips nothing, and a later live run with the same evidence
  still executes (the shared shadow/live run-key footgun).

Evidence arrives via ``--param evidence_file`` (normalized JSON) so no eval
touches the network; the live client is exercised in tests/unit.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status, check_ref)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, check_ref in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
            )
            if check_ref:
                store.record_payment_details(ledger, invoice_id=inv_id, check_ref=check_ref)
    return root


def _evidence_file(tmp_path: Path, *entries) -> Path:
    """entries: dicts with qbo_id/payee/amount_cents/date/check_ref/txn_type"""
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(list(entries)))
    return p


def _run(ledger_dir: Path, evidence: Path, *, shadow: bool = False):
    return run(
        "demo",
        "ap",
        "reconcile",
        shadow=shadow,
        params={"evidence_file": str(evidence)},
        ledger_dir=ledger_dir,
    )


def _row(root, number):
    with Ledger.open(root) as ledger:
        rows = store.invoices_by_number(ledger, "demo", number)
    return rows[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


PAYMENT = {
    "qbo_id": "BillPayment:301",
    "txn_type": "BillPayment",
    "payee": VENDOR,
    "amount_cents": 12500,
    "date": "2026-07-15",
    "check_ref": "",
}


def test_clean_match_flips_scheduled_to_paid_with_payment_details(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled", ""))
    ev = _evidence_file(tmp_path, PAYMENT)

    result = _run(d, ev)

    assert result.status == "ok"
    row = _row(root, "N-1")
    assert row["status"] == "Paid"
    assert row["payment_date"] == "2026-07-15"
    (paid,) = _events(root, "ap.reconcile.paid")
    assert paid["payload"]["qbo_id"] == "BillPayment:301"


def test_grouped_check_settles_all_rows_when_the_sum_agrees(tmp_path):
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "G-1", 200000, "Scheduled", "Check 9051"),
        (VENDOR, "G-2", 30000, "Scheduled", "Check 9051"),
    )
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:201",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 230000,
            "date": "2026-07-14",
            "check_ref": "9051",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "G-1")["status"] == "Paid"
    assert _row(root, "G-2")["status"] == "Paid"
    assert len(_events(root, "ap.reconcile.paid")) == 2


def test_grouped_check_with_sum_mismatch_parks_and_flips_nothing(tmp_path):
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "G-1", 200000, "Scheduled", "Check 9051"),
        (VENDOR, "G-2", 30000, "Scheduled", "Check 9051"),
    )
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:202",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 200000,  # covers G-1 alone, but the ref names the pair
            "date": "2026-07-14",
            "check_ref": "9051",
        },
    )

    result = _run(d, ev)

    assert result.status == "needs_approval"
    assert _row(root, "G-1")["status"] == "Scheduled"
    assert _row(root, "G-2")["status"] == "Scheduled"


def test_ambiguous_amount_parks_approval_and_flips_nothing(tmp_path):
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "A-1", 12500, "Scheduled", ""),
        (VENDOR, "A-2", 12500, "Scheduled", ""),
    )
    ev = _evidence_file(tmp_path, PAYMENT)

    result = _run(d, ev)

    assert result.status == "needs_approval"
    (card,) = result.approvals_needed
    assert card.action_type == "ap.reconcile_review"
    assert _row(root, "A-1")["status"] == "Scheduled"
    assert _row(root, "A-2")["status"] == "Scheduled"
    # Parking is a recorded fact (#136): the run's memory of the ambiguity
    # lives in the event log, not only in the mutable approval queue.
    parked = _events(root, "ap.reconcile.review_parked")
    assert len(parked) == 1
    assert parked[0]["payload"]["qbo_id"] == PAYMENT["qbo_id"]


def test_cleared_payment_with_no_open_row_is_an_anomaly_never_a_row(tmp_path):
    """The 2026-05-19/2026-05-21 class: unknown money movement gets flagged,
    the engine never fabricates a ledger row or guesses a match."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "P-1", 99900, "Paid", ""))  # vendor known, nothing open
    ev = _evidence_file(tmp_path, PAYMENT)

    result = _run(d, ev)

    assert any(a.code == "ap.reconcile.unknown_payment" for a in result.anomalies)
    with Ledger.open(root) as ledger:
        count = ledger.conn.execute("SELECT COUNT(*) FROM ap_invoices").fetchone()[0]
    assert count == 1  # no fabricated rows


def test_orphan_check_with_unknown_payee_is_still_an_anomaly(tmp_path):
    d = tmp_path / "d"
    _seed(d, (VENDOR, "P-1", 99900, "Paid", ""))
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:207",
            "txn_type": "Purchase",
            "payee": "",
            "amount_cents": 55500,
            "date": "2026-07-15",
            "check_ref": "7777",
        },
    )

    result = _run(d, ev)

    assert any(a.code == "ap.reconcile.unknown_payment" for a in result.anomalies)


def test_ignored_recurring_payee_is_out_of_scope_despite_ledger_presence(tmp_path):
    """Bank fees and payroll processors clear every month and are the bank
    rules' business; without the ignore list each new clearing would flag as
    unknown money once the payee has any ledger history."""
    d = tmp_path / "d"
    _seed(d, ("First Example Bank", "FEE-1", 5000, "Paid", ""))
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:401",
            "txn_type": "Purchase",
            "payee": "First Example Bank",
            "amount_cents": 5000,
            "date": "2026-08-10",
            "check_ref": "",
        },
    )

    result = run(
        "demo",
        "ap",
        "reconcile",
        shadow=False,
        params={"evidence_file": str(ev), "ignore_payees": "First Example Bank"},
        ledger_dir=d,
    )

    assert result.status == "ok"
    assert result.anomalies == []
    assert "out of scope: 1" in result.summary


def test_out_of_scope_card_spend_is_counted_not_flagged(tmp_path):
    d = tmp_path / "d"
    _seed(d, (VENDOR, "P-1", 99900, "Paid", ""))
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:208",
            "txn_type": "Purchase",
            "payee": "Roadside Coffee",
            "amount_cents": 425,
            "date": "2026-07-15",
            "check_ref": "",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert result.anomalies == []
    assert result.approvals_needed == []
    assert "out of scope: 1" in result.summary


def test_payable_row_that_cleared_settles_but_flags_the_lag(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "L-1", 12500, "Received", ""))
    ev = _evidence_file(tmp_path, PAYMENT)

    result = _run(d, ev)

    assert _row(root, "L-1")["status"] == "Paid"  # settled money is settled
    assert any(a.code == "ap.reconcile.status_lag" for a in result.anomalies)


def test_rerun_causes_no_further_mutation_then_noops(tmp_path):
    """Safe-to-re-run, precisely: the settling run changes the ledger, so the
    NEXT run legitimately executes, but it must recognize the evidence as
    already explained and touch nothing (no duplicate flip, no false unknown-
    payment anomaly). Only then, with truly identical inputs, does the runner
    replay a noop."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled", ""))
    ev = _evidence_file(tmp_path, PAYMENT)

    first = _run(d, ev)
    second = _run(d, ev)
    third = _run(d, ev)

    assert first.status == "ok"
    assert second.status == "ok"
    assert second.anomalies == []  # the settled payment is not "unknown money"
    assert len(_events(root, "ap.reconcile.paid")) == 1  # no duplicate flip
    assert third.status == "noop"


def test_payment_already_recorded_on_a_settled_row_never_flips_a_twin(tmp_path):
    """2026-07-16 live-shadow catch: a vendor paid 50/50 produces two
    identical-amount payments. The June clearing belonged to an already-Paid
    row; the matcher must recognize that and leave the OPEN twin (whose own
    check is still in the mail) alone. Not an anomaly either: the book
    already explains this money."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "HALF-1", 796484, "Paid", "9054"),
        (VENDOR, "HALF-2", 796484, "Scheduled", ""),
    )
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "HALF-1")[0]
        store.record_payment_details(ledger, invoice_id=row["id"], payment_date="2026-06-24")
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:212",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 796484,
            "date": "2026-06-24",
            "check_ref": "9054",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "HALF-2")["status"] == "Scheduled"  # the twin stays open
    assert result.anomalies == []
    assert "already recorded: 1" in result.summary


def test_settled_group_under_one_check_explains_the_combined_payment(tmp_path):
    """One check paid two invoices; both rows are already Paid. The combined
    clearing is explained by the settled group, not unknown money."""
    d = tmp_path / "d"
    _seed(
        d,
        (VENDOR, "0941", 200000, "Paid", "Check 9050"),
        (VENDOR, "0943", 201000, "Paid", "Check 9050"),
    )
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:192",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 401000,
            "date": "2026-06-18",
            "check_ref": "9050",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert result.anomalies == []
    assert "already recorded: 1" in result.summary


def test_settled_row_explains_a_clearing_weeks_after_its_payment_date(tmp_path):
    """A mailed check routinely clears 1-3 weeks after the book's payment
    date (written 7/7, deposited late, cleared 7/16+). The settled-row
    window must tolerate that lag or every slow-deposited check re-flags as
    unknown money once the accounting book catches up."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "SLOW-1", 230000, "Paid", "Check 9051"))
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "SLOW-1")[0]
        store.record_payment_details(ledger, invoice_id=row["id"], payment_date="2026-07-07")
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:230",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 230000,
            "date": "2026-07-24",  # 17 days after the recorded payment date
            "check_ref": "9051",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert result.anomalies == []
    assert "already recorded: 1" in result.summary


def test_rows_born_in_shadow_intake_are_real_match_candidates(tmp_path):
    """Pre-cutover shadow intake created rows that ARE the live book (the
    2026-07-16 D&E-class miss). Reconcile must treat them like any row."""
    d = tmp_path / "d"
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor=VENDOR,
            invoice_number="SH-1",
            amount_cents=50500,
            status="Scheduled",
            shadow=True,
        )
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:219",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 50500,
            "date": "2026-07-01",
            "check_ref": "7060",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "SH-1")["status"] == "Paid"


def test_clearing_dated_before_the_rows_commitment_parks_for_review(tmp_path):
    """A payment that cleared before the row was even scheduled cannot be
    that row's payment (identical-amount twin defense when no settled row
    explains it). Park it; never guess."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "E-1", 796484, "Received", ""))
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "E-1")[0]
        store.update_status(ledger, invoice_id=row["id"], status_to="Scheduled", actor="owner")
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:213",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 796484,
            "date": "2020-01-01",  # long before the row was committed
            "check_ref": "",
        },
    )

    result = _run(d, ev)

    assert result.status == "needs_approval"
    assert _row(root, "E-1")["status"] == "Scheduled"


def test_shadow_flips_nothing_and_live_still_executes_afterwards(tmp_path):
    """Shadow and live must not share a run key: the whole point of shadow is
    a dry look at the SAME inputs the live run will then act on."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "S-1", 12500, "Scheduled", ""))
    ev = _evidence_file(tmp_path, PAYMENT)

    dry = _run(d, ev, shadow=True)

    assert dry.status == "ok"
    assert _row(root, "S-1")["status"] == "Scheduled"  # untouched
    assert _events(root, "ap.reconcile.paid") == []  # no false history
    assert any("would settle" in a for a in dry.actions)  # the report exists

    live = _run(d, ev)

    assert live.status == "ok"  # NOT a noop replay of the shadow run
    assert _row(root, "S-1")["status"] == "Paid"


# ---- issue #134: a settled twin must never absorb a DIFFERENT check ----------


def _split_pair(tmp_path, *, open_ref=""):
    """The live split-payment shape: HALF-1 Paid via check 9054 (6/24),
    HALF-2 still open, optionally carrying its own recorded check ref."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "HALF-1", 796484, "Paid", "9054"),
        (VENDOR, "HALF-2", 796484, "Scheduled", open_ref),
    )
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "HALF-1")[0]
        store.record_payment_details(ledger, invoice_id=row["id"], payment_date="2026-06-24")
    return d, root


def test_2026_08_20_second_check_of_split_payment_settles_the_open_twin(tmp_path):
    """Issue #134 incident eval. HALF-2's own check (9055) clears inside
    HALF-1's date window. The payee tier used to absorb it as
    already-recorded and HALF-2 stayed Scheduled forever with zero
    anomalies; the check number is the strongest reference and must win."""
    d, root = _split_pair(tmp_path, open_ref="9055")
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:900",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 796484,
            "date": "2026-07-05",
            "check_ref": "9055",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "HALF-2")["status"] == "Paid"
    assert _row(root, "HALF-2")["payment_date"] == "2026-07-05"
    assert "already recorded: 0" in result.summary


def test_conflicting_check_never_absorbs_even_without_a_ref_on_the_open_twin(tmp_path):
    """Same shape, but the operator never stamped HALF-2's check ref. The
    evidence's 9055 conflicts with the settled twin's 9054, so the payee
    absorb steps aside and payee+amount settles the open twin."""
    d, root = _split_pair(tmp_path, open_ref="")
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:901",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 796484,
            "date": "2026-07-05",
            "check_ref": "9055",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "HALF-2")["status"] == "Paid"
    assert "already recorded: 0" in result.summary


def test_checkless_clearing_is_still_absorbed_by_the_settled_twin(tmp_path):
    """The 2026-07-16 protection stands: evidence with NO check number and a
    settled same-amount twin in window is already-recorded money; the open
    twin (own check still in the mail) stays untouched."""
    d, root = _split_pair(tmp_path, open_ref="")
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:902",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 796484,
            "date": "2026-06-25",
            "check_ref": "",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "HALF-2")["status"] == "Scheduled"
    assert result.anomalies == []
    assert "already recorded: 1" in result.summary


def test_no_payment_date_settled_twin_with_conflicting_check_does_not_absorb(tmp_path):
    """A settled row that never recorded a payment date is date-compatible
    with everything; its conflicting check number must still disqualify the
    absorb or it eats every same-amount clearing forever."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "HALF-1", 796484, "Paid", "9054"),  # no payment_date recorded
        (VENDOR, "HALF-2", 796484, "Scheduled", "9055"),
    )
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:903",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 796484,
            "date": "2026-07-05",
            "check_ref": "9055",
        },
    )

    result = _run(d, ev)

    assert result.status == "ok"
    assert _row(root, "HALF-2")["status"] == "Paid"
    assert "already recorded: 0" in result.summary
