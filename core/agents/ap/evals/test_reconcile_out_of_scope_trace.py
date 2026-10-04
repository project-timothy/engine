"""ap/reconcile: a material clearing never vanishes into the out-of-scope counter.

The 2026-09-14 check-3048 run-down. Check 3048 (a five-figure owner-side payment
payoff) cleared the bank on 2026-07-07, was booked correctly in QBO, and
left NO trace in the engine. QBO's bank-feed import gave the row no payee
and no DocNumber, so ``decide`` could not call it unknown (step 3 needs a
check ref or a ledger-known payee) and dropped it to ``out_of_scope`` --
whose entire handling was ``out_of_scope += 1``. The run summary said
"out of scope: N" and N was the only record that $13,750 had moved.

``out_of_scope`` is the right sink for cardswipe spend; the bug is that it
is a sink for EVERYTHING that falls past the rules, at any size. These
evals pin the floor:

- at or above the trace floor, the clearing emits ``ap.reconcile.out_of_scope``
  carrying enough identity to run it down (id, amount, date, payee, check);
- below the floor, the counter still swallows it (Bucks Tavern stays quiet);
- a payee on the curated ignore list stays silent at ANY amount: that list is
  a deliberate owner decision, not a rule falling through;
- shadow mode writes nothing, and a later live run still emits;
- the sink leaving a trace changes no settle/review/unknown outcome.

A floor of 0 traces every out-of-scope clearing.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"

# The 3048 shape: real money, no payee, no check number, nothing to match.
LOAN_PAYOFF = {
    "qbo_id": "Purchase:223",
    "txn_type": "Purchase",
    "payee": "",
    "amount_cents": 1375000,
    "date": "2026-07-07",
    "check_ref": "",
}
# The noise the sink exists for.
CARDSWIPE = {
    "qbo_id": "Purchase:222",
    "txn_type": "Purchase",
    "payee": "",
    "amount_cents": 3525,
    "date": "2026-07-10",
    "check_ref": "",
}


def _seed(ledger_dir: Path, *rows):
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status in rows:
            store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
            )
    return root


def _evidence_file(tmp_path: Path, *entries) -> Path:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(list(entries)))
    return p


def _run(ledger_dir: Path, evidence: Path, *, shadow: bool = False, **params):
    return run(
        "demo",
        "ap",
        "reconcile",
        shadow=shadow,
        params={"evidence_file": str(evidence), **params},
        ledger_dir=ledger_dir,
    )


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def test_material_out_of_scope_clearing_leaves_a_trace(tmp_path):
    """The $13,750 that vanished on 2026-07-07 now names itself."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled"))
    ev = _evidence_file(tmp_path, LOAN_PAYOFF)

    result = _run(d, ev, trace_floor_cents="100000")

    assert result.status == "ok"
    (traced,) = _events(root, "ap.reconcile.out_of_scope")
    payload = traced["payload"]
    assert payload["qbo_id"] == "Purchase:223"
    assert payload["amount_cents"] == 1375000
    assert payload["date"] == "2026-07-07"
    # Identity the run-down needed: the empty fields are recorded as empty,
    # never omitted, because "QBO told us nothing" is itself the finding.
    assert payload["payee"] == ""
    assert payload["check_ref"] == ""


def test_below_the_floor_the_counter_still_swallows_it(tmp_path):
    """Cardswipe noise is what the sink is for; it stays quiet."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled"))
    ev = _evidence_file(tmp_path, CARDSWIPE)

    result = _run(d, ev, trace_floor_cents="100000")

    assert result.status == "ok"
    assert _events(root, "ap.reconcile.out_of_scope") == []
    assert "out of scope: 1" in result.summary


def test_a_zero_floor_traces_every_out_of_scope_clearing(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled"))
    ev = _evidence_file(tmp_path, LOAN_PAYOFF, CARDSWIPE)

    _run(d, ev, trace_floor_cents="0")

    traced = {e["payload"]["qbo_id"] for e in _events(root, "ap.reconcile.out_of_scope")}
    assert traced == {"Purchase:223", "Purchase:222"}


def test_an_ignored_payee_stays_silent_at_any_amount(tmp_path):
    """The curated ignore list is an owner decision, not a rule falling
    through. Tracing it would re-introduce exactly the noise it removes."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled"))
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:999",
            "txn_type": "Purchase",
            "payee": "Payroll Co",
            "amount_cents": 5000000,
            "date": "2026-07-15",
            "check_ref": "",
        },
    )

    _run(d, ev, ignore_payees="Payroll Co", trace_floor_cents="0")

    assert _events(root, "ap.reconcile.out_of_scope") == []


def test_shadow_writes_no_trace_and_a_later_live_run_still_emits(tmp_path):
    """The shared shadow/live run-key footgun, on the new event."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled"))
    ev = _evidence_file(tmp_path, LOAN_PAYOFF)

    _run(d, ev, shadow=True, trace_floor_cents="100000")
    assert _events(root, "ap.reconcile.out_of_scope") == []

    _run(d, ev, trace_floor_cents="100000")
    assert len(_events(root, "ap.reconcile.out_of_scope")) == 1


def test_tracing_changes_no_settle_outcome(tmp_path):
    """The sink speaking up must not move money or touch a row."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Scheduled"))
    ev = _evidence_file(
        tmp_path,
        LOAN_PAYOFF,
        {
            "qbo_id": "BillPayment:301",
            "txn_type": "BillPayment",
            "payee": VENDOR,
            "amount_cents": 12500,
            "date": "2026-07-15",
            "check_ref": "",
        },
    )

    _run(d, ev, trace_floor_cents="100000")

    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "N-1")[0]
    assert row["status"] == "Paid"
    assert len(_events(root, "ap.reconcile.paid")) == 1
    assert len(_events(root, "ap.reconcile.out_of_scope")) == 1
