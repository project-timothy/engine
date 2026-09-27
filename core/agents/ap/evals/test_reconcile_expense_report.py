"""ap/reconcile: the engine's own expense-report Purchase is not unknown money.

Issue #281, from the 2026-09-17 triage. Two expense reports cleared the bank
and each produced a ``reconcile unknown-clearing`` WARN the owner had to mute
by hand: Purchase 275 (the owner reimbursement, muted 2026-09-11) and
Purchase 304 (the vendor-role report, muted 2026-09-17). Every future report
would have done the same, forever.

``decide`` resolved a cleared payment against ``ap_invoices`` only, and an
expense report never has a row there: the money is committed by the report
itself. ``expense_report.qbo_purchase_id`` holds the exact QBO id the engine
wrote at ``expenses match``, and nothing consulted it.

What these evals pin:

- both live specimens decide ``already_recorded``, attributed to their report,
  with zero unknown events;
- the match is the EXACT Purchase id, never the amount: a report whose
  Purchase is a different record explains nothing;
- identity answers BEFORE the amount heuristics: one payee can be both an AP
  vendor with open rows and an expense-report person, and an open row at the
  same amount must not be settled by the report's own Purchase;
- an amount disagreement between the clearing and the report total (304 was
  trimmed to $5,689.28 the day after the report recorded $5,731.18) is a
  bookkeeping note recorded ONCE, never a nightly nag;
- an owner on the curated ignore list is still attributed to his report
  instead of falling into the out-of-scope counter;
- a Purchase no report and no row explains is still unknown money.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.agents.ap import store
from core.agents.ap.reconcile import decide
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"
DRIFT_EVENT = "ap.reconcile.expense_amount_drift"

# Purchase 275: the owner's EXP-1, paid by check 3050. Report and clearing
# agree to the cent.
EXP_1 = {
    "qbo_id": "Purchase:275",
    "txn_type": "Purchase",
    "payee": "Pat Owner",
    "amount_cents": 207453,
    "date": "2026-08-12",
    "check_ref": "EXP-1",
}
EXP_1_TOTAL = 207453
# Purchase 304: the vendor-role EXP-2. The owner trimmed the Purchase in the
# accounting UI the day after the engine wrote it; the report still holds the
# receipts-based total.
EXP_2 = {
    "qbo_id": "Purchase:304",
    "txn_type": "Purchase",
    "payee": "Keystone Fabrication LLC",
    "amount_cents": 568928,
    "date": "2026-09-15",
    "check_ref": "EXP-2",
}
EXP_2_TOTAL = 573118


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status)"""
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


def _seed_report(root, *, report_id: int, person: str, total_cents: int, purchase_id: str):
    """One expense report the engine already recorded in the accounting
    system, exactly as ``expenses match`` leaves it."""
    with Ledger.open(root) as ledger:
        ledger.conn.execute(
            "INSERT INTO expense_report (id, idempotency_key, tenant, person, month, "
            "total_cents, status, created_at, updated_at, qbo_purchase_id) "
            "VALUES (?, ?, 'demo', ?, '2026-09', ?, 'Reimbursed-Recorded', "
            "'2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z', ?)",
            (report_id, f"exp-{report_id}", person, total_cents, purchase_id),
        )
        ledger.conn.commit()


def _evidence_file(tmp_path: Path, *entries, name: str = "evidence.json") -> Path:
    p = tmp_path / name
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


def _both_reports(ledger_dir: Path):
    root = _seed(ledger_dir, (VENDOR, "N-1", 12500, "Scheduled"))
    _seed_report(root, report_id=1, person="Pat Owner", total_cents=EXP_1_TOTAL, purchase_id="275")
    _seed_report(root, report_id=2, person="Sam Vendor", total_cents=EXP_2_TOTAL, purchase_id="304")
    return root


def test_both_live_specimens_are_already_recorded(tmp_path):
    """275 and 304, the two clearings the owner had to mute by hand."""
    d = tmp_path / "d"
    root = _both_reports(d)
    ev = _evidence_file(tmp_path, EXP_1, EXP_2)

    result = _run(d, ev)

    assert result.status == "ok"
    assert _events(root, "ap.reconcile.unknown") == []
    assert "unknown 0" in result.summary
    assert "already recorded: 2" in result.summary


def test_the_decision_names_the_report():
    """The clearing is attributed, not merely absorbed: the decision carries
    the report id so the event can say which report this money was."""
    decision = decide(
        SimpleNamespace(**EXP_2),
        [],
        expense_purchases={"304": {"id": 2, "total_cents": EXP_2_TOTAL}},
    )

    assert decision.kind == "already_recorded"
    assert decision.expense_report_id == 2


def test_the_match_is_the_exact_purchase_id_never_the_amount():
    """Exact key, no heuristic: a report that recorded a DIFFERENT Purchase
    explains nothing, however well the amount agrees."""
    decision = decide(
        SimpleNamespace(**EXP_2),
        [],
        expense_purchases={"275": {"id": 1, "total_cents": EXP_2["amount_cents"]}},
    )

    assert decision.kind != "already_recorded"
    assert decision.expense_report_id is None


def test_an_open_row_for_the_same_payee_and_amount_is_left_alone(tmp_path):
    """The collision the rule position exists for. The expense-report person
    is also a vendor with open AP rows (one entity bills the business AND is
    reimbursed), and one of those rows carries the cleared amount to the
    cent. Under the payee-plus-amount rule that row would be settled by the
    report's own Purchase: money recorded twice, one real payable wrongly
    closed. The exact id answers first, so the row is untouched."""
    d = tmp_path / "d"
    root = _seed(d, ("Keystone Fabrication LLC", "KF-77", EXP_2["amount_cents"], "Scheduled"))
    _seed_report(root, report_id=2, person="Sam Vendor", total_cents=EXP_2_TOTAL, purchase_id="304")
    ev = _evidence_file(tmp_path, EXP_2)

    result = _run(d, ev)

    assert "settled 0" in result.summary
    assert "already recorded: 1" in result.summary
    assert _events(root, "ap.reconcile.paid") == []
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "KF-77")[0]
    assert row["status"] == "Scheduled"

    # And the pure decision says whose money it was.
    decision = decide(
        SimpleNamespace(**EXP_2),
        [
            {
                "id": 1,
                "vendor": "Keystone Fabrication LLC",
                "invoice_number": "KF-77",
                "amount_cents": EXP_2["amount_cents"],
                "status": "Scheduled",
                "check_ref": "",
                "payment_date": None,
            }
        ],
        expense_purchases={"304": {"id": 2, "total_cents": EXP_2_TOTAL}},
    )
    assert decision.kind == "already_recorded"
    assert decision.expense_report_id == 2
    assert decision.rows == []


def test_a_bill_payment_sharing_the_number_is_a_different_record():
    """Accounting-system ids are per entity: BillPayment 304 and Purchase 304
    can both exist. The rule reads the whole id, entity included."""
    decision = decide(
        SimpleNamespace(**{**EXP_2, "qbo_id": "BillPayment:304", "txn_type": "BillPayment"}),
        [],
        expense_purchases={"304": {"id": 2, "total_cents": EXP_2_TOTAL}},
    )

    assert decision.kind == "unknown"
    assert decision.expense_report_id is None


def test_a_purchase_no_report_and_no_row_explains_is_still_unknown(tmp_path):
    """The 2026-05-19 orphan-check class survives this fix untouched."""
    d = tmp_path / "d"
    root = _both_reports(d)
    orphan = {
        "qbo_id": "Purchase:999",
        "txn_type": "Purchase",
        "payee": VENDOR,
        "amount_cents": 990000,
        "date": "2026-09-15",
        "check_ref": "1099",
    }
    ev = _evidence_file(tmp_path, orphan)

    result = _run(d, ev)

    (unknown,) = _events(root, "ap.reconcile.unknown")
    assert unknown["payload"]["qbo_id"] == "Purchase:999"
    assert "unknown 1" in result.summary


def test_an_amount_disagreement_is_recorded_once_and_never_nags(tmp_path):
    """The $41.90 shape. The money IS recorded; only the number moved, so it
    is an informational event with an idempotent guard, not an anomaly and
    not a nightly checklist item."""
    d = tmp_path / "d"
    root = _both_reports(d)
    ev = _evidence_file(tmp_path, EXP_2)

    result = _run(d, ev)

    assert result.anomalies == []
    (drift,) = _events(root, DRIFT_EVENT)
    payload = drift["payload"]
    assert payload["qbo_id"] == "Purchase:304"
    assert payload["expense_report_id"] == 2
    assert payload["report_total_cents"] == EXP_2_TOTAL
    assert payload["cleared_amount_cents"] == EXP_2["amount_cents"]
    assert payload["drift_cents"] == EXP_2_TOTAL - EXP_2["amount_cents"]

    # A later run that really executes (new evidence, new run key) must not
    # say it again: the guard is the event log, not the run key.
    later = _evidence_file(
        tmp_path,
        EXP_2,
        EXP_1,
        name="later.json",
    )
    second = _run(d, later)

    assert second.status == "ok"  # executed, not replayed
    assert len(_events(root, DRIFT_EVENT)) == 1


def test_an_agreeing_report_records_no_drift(tmp_path):
    """275 ties to the cent, so it says nothing at all."""
    d = tmp_path / "d"
    root = _both_reports(d)
    ev = _evidence_file(tmp_path, EXP_1)

    _run(d, ev)

    assert _events(root, DRIFT_EVENT) == []


def test_shadow_writes_no_drift_and_a_later_live_run_still_emits(tmp_path):
    """The shared shadow/live run-key footgun, on the new event."""
    d = tmp_path / "d"
    root = _both_reports(d)
    ev = _evidence_file(tmp_path, EXP_2)

    _run(d, ev, shadow=True)
    assert _events(root, DRIFT_EVENT) == []

    _run(d, ev)
    assert len(_events(root, DRIFT_EVENT)) == 1


def test_an_ignored_payee_is_still_attributed_to_his_report(tmp_path):
    """Purchase 275's payee is an owner, and owners sit on the curated
    ignore list: without this the report's own clearing would vanish into
    the out-of-scope counter with nothing attributed and no drift check."""
    d = tmp_path / "d"
    root = _both_reports(d)
    ev = _evidence_file(tmp_path, EXP_1)

    result = _run(d, ev, ignore_payees="Pat Owner")

    assert "already recorded: 1" in result.summary
    assert "out of scope: 0" in result.summary
    assert _events(root, "ap.reconcile.unknown") == []


def test_the_drift_event_key_names_the_same_three_facts_the_guard_reads(tmp_path):
    """#313, closed 2026-09-18 as not a live defect (the run key already
    carries the report total, so a changed total never collides across
    runs), kept as a consistency rule: the guard set is keyed on
    ``<qbo id>:<report cents>:<cleared cents>`` and the event's own key
    names the same three facts, so a reader of the log sees which report
    total the note was about without opening the payload."""
    d = tmp_path / "d"
    root = _both_reports(d)

    _run(d, _evidence_file(tmp_path, EXP_2))

    with Ledger.open(root) as ledger:
        (row,) = ledger.conn.execute(
            "SELECT idempotency_key FROM events WHERE event_type = ?", (DRIFT_EVENT,)
        ).fetchall()
    assert row["idempotency_key"].endswith(
        f":evt:expense_drift:Purchase:304:{EXP_2_TOTAL}:{EXP_2['amount_cents']}"
    )
