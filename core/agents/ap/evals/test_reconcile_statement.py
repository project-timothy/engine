"""ap/reconcile: a bank statement file is clearing evidence beside QBO
(phase 7 row 7.3, issue #212).

Once the engine records a BillPayment itself (row 7.2), the owner's later
Match click in the bank feed is invisible to the API and reconcile filters
the engine's own record out of QBO evidence by construction (write-side
rule 3). The statement file is the only thing that settles those rows.

Contract under test (the row's acceptance):

- a statement line matching an engine-written payment on normalized check
  reference, amount (the group sum), and a date window around the recorded
  payment date flips EVERY row sharing that ``qbo_payment_id`` to Paid with
  ``evidence=statement``, the statement's cleared date, and the check
  reference, emitting ``ap.reconcile.paid`` per row (``qbo_id`` = the
  engine's BillPayment, the record the auditor can verify) and
  ``ap.invoice.paid`` per row (the 7.1 shape);
- a check line matching nothing raises ``ap.reconcile.unknown`` exactly
  once across re-runs, keyed on the line's identity (date + amount + ref);
- a line two candidate groups could explain parks an ``ap.reconcile_review``
  card in the 7.1 shape (structured ``payments``) and flips nothing; the
  owner's ``--param row=`` answer executes on the next run;
- QBO-evidence behavior is unchanged (every existing reconcile eval stays
  green untouched; a check QBO already flagged unknown is not flagged twice);
- ``--param bank_csv`` absent, or naming a file that is not there, skips the
  statement tier with one log line and no anomaly;
- a second run over the same file writes nothing, and the run key declares
  the file by content: a changed statement re-executes, an unchanged one
  (even re-touched) replays.

Evidence arrives via ``--param evidence_file`` and the statement via a
``tmp_path`` CSV in the demo tenant's ``[bank_csv]`` format, so no eval
touches the network or an owner folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.agents.ap import store
from core.engine.cli import main
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"
OTHER = "Beta Supply"
CARD = "ap.reconcile_review"
PAID = "ap.reconcile.paid"
ROW_PAID = "ap.invoice.paid"
UNKNOWN = "ap.reconcile.unknown"
PARKED = "ap.reconcile.review_parked"
PAY_DATE = "2026-09-10"  # the day the owner recorded the check at scheduling

# The demo tenant's deliberately different export format (Posted/Memo/Chk/Value,
# ISO dates): proves the statement tier reads through the config-driven adapter.
HEADER = "Posted,Memo,Chk,Value\n"

# The engine's own BillPayment as QBO would return it: present in QBO evidence
# and, by rule 3, never clearing evidence for the rows it was written for.
ENGINE_BILLPAYMENT = {
    "qbo_id": "BillPayment:BP1",
    "txn_type": "BillPayment",
    "payee": VENDOR,
    "amount_cents": 12500,
    "date": PAY_DATE,
    "check_ref": "9058",
    "linked_bill_ids": ["B1", "B2"],
}


def _csv(tmp_path: Path, *lines, name: str = "statement.csv") -> Path:
    """lines: (posted, memo, chk, value) in the demo format."""
    p = tmp_path / name
    p.write_text(HEADER + "".join(f"{d},{m},{c},{v}\n" for d, m, c, v in lines))
    return p


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status, check_ref, bill_id, payment_id)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, check_ref, bill_id, payment_id in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
                invoice_date="2026-08-20",
            )
            if check_ref:
                store.record_payment_details(
                    ledger, invoice_id=inv_id, payment_date=PAY_DATE, check_ref=check_ref
                )
            if bill_id or payment_id:
                store.record_qbo_ids(
                    ledger, invoice_id=inv_id, bill_id=bill_id, payment_id=payment_id
                )
    return root


def _evidence_file(tmp_path: Path, *entries) -> Path:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(list(entries)))
    return p


def _run(ledger_dir: Path, evidence: Path, *, bank_csv=None, shadow: bool = False, **extra):
    params = {"evidence_file": str(evidence), **extra}
    if bank_csv is not None:
        params["bank_csv"] = str(bank_csv)
    return run("demo", "ap", "reconcile", shadow=shadow, params=params, ledger_dir=ledger_dir)


def _row(root, number):
    with Ledger.open(root) as ledger:
        return store.invoices_by_number(ledger, "demo", number)[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _cards(root, status=None):
    with Ledger.open(root) as ledger:
        return [c for c in ledger.list_approvals("demo", status=status) if c["action_type"] == CARD]


def _history_count(root) -> int:
    with Ledger.open(root) as ledger:
        return ledger.conn.execute("SELECT COUNT(*) FROM ap_status_history").fetchone()[0]


def _approve(ledger_dir: Path, card_id: int, capsys, **params):
    argv = ["queue", "approve", "demo", "--id", str(card_id), "--ledger-dir", str(ledger_dir)]
    for k, v in params.items():
        argv += ["--param", f"{k}={v}"]
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _engine_check(tmp_path):
    """The 9058 shape: one engine-written BillPayment covering two rows."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "N-1", 10000, "Scheduled", "9058", "B1", "BillPayment:BP1"),
        (VENDOR, "N-2", 2500, "Scheduled", "9058", "B2", "BillPayment:BP1"),
    )
    return d, root


# ---- the acceptance ----------------------------------------------------------


def test_statement_line_settles_every_row_sharing_the_engine_payment(tmp_path):
    d, root = _engine_check(tmp_path)
    ev = _evidence_file(tmp_path, ENGINE_BILLPAYMENT)
    csv = _csv(tmp_path, ("2026-09-16", "CHECK 9058", "9058", "-125.00"))

    result = _run(d, ev, bank_csv=csv)

    assert result.status == "ok", result.summary
    for number in ("N-1", "N-2"):
        row = _row(root, number)
        assert row["status"] == "Paid"
        assert row["payment_date"] == "2026-09-16"  # the statement's cleared date
        assert row["check_ref"] == "9058"
    paid = _events(root, PAID)
    assert len(paid) == 2
    assert {p["payload"]["evidence"] for p in paid} == {"statement"}
    # The auditor's QBO lens verifies each paid event's qbo_id against the
    # accounting system: the engine's own BillPayment is the record there.
    assert {p["payload"]["qbo_id"] for p in paid} == {"BillPayment:BP1"}
    assert all(p["payload"]["payment_date"] == "2026-09-16" for p in paid)
    assert all(p["payload"]["check_ref"] == "9058" for p in paid)
    assert all(p["payload"]["statement_id"].startswith("stmt:") for p in paid)
    assert sum(p["payload"]["amount_cents"] for p in paid) == 12500
    row_paid = _events(root, ROW_PAID)
    assert len(row_paid) == 2
    assert {e["payload"]["resolved_by"] for e in row_paid} == {"statement"}
    assert _events(root, UNKNOWN) == []
    assert result.anomalies == []


def test_line_matching_nothing_raises_unknown_exactly_once_across_reruns(tmp_path):
    d, root = _engine_check(tmp_path)
    ev = _evidence_file(tmp_path)
    csv = _csv(tmp_path, ("2026-09-16", "CHECK 7777", "7777", "-555.00"))

    first = _run(d, ev, bank_csv=csv)

    assert first.status == "ok"
    assert [a.code for a in first.anomalies] == ["ap.reconcile.unknown_payment"]
    (unknown,) = _events(root, UNKNOWN)
    assert unknown["payload"]["check_ref"] == "7777"
    assert unknown["payload"]["amount_cents"] == 55500
    assert unknown["payload"]["date"] == "2026-09-16"
    assert unknown["payload"]["source"] == "statement"
    assert unknown["payload"]["statement_id"].startswith("stmt:")
    assert unknown["payload"]["qbo_id"] == unknown["payload"]["statement_id"]

    second = _run(d, ev, bank_csv=csv)
    assert second.status == "noop"  # same inputs, replayed
    third = _run(d, ev, bank_csv=csv, ignore_payees="Nobody")  # a new key executes again
    assert third.status == "ok"
    assert third.anomalies == []  # the line is remembered as explained
    assert len(_events(root, UNKNOWN)) == 1
    assert _row(root, "N-1")["status"] == "Scheduled"


def test_line_matching_two_candidate_groups_parks_a_review_card(tmp_path, capsys):
    """A reused check number: two engine payments share the reference and
    the amount. The engine never guesses; the owner picks the row."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "A-1", 10000, "Scheduled", "3050", "B1", "BillPayment:BP1"),
        (OTHER, "B-1", 10000, "Scheduled", "3050", "B2", "BillPayment:BP2"),
    )
    ev = _evidence_file(tmp_path)
    csv = _csv(tmp_path, ("2026-09-16", "CHECK 3050", "3050", "-100.00"))

    result = _run(d, ev, bank_csv=csv)

    assert result.status == "needs_approval"
    assert _row(root, "A-1")["status"] == "Scheduled"
    assert _row(root, "B-1")["status"] == "Scheduled"
    (card,) = _cards(root, "pending")
    params = card["params"]
    (payment,) = params["payments"]
    assert payment["amount_cents"] == 10000
    assert payment["date"] == "2026-09-16"
    assert payment["check_ref"] == "3050"
    assert payment["source"] == "statement"
    assert payment["qbo_id"].startswith("stmt:")
    assert params["statement_id"] == payment["qbo_id"]
    assert "#" in params["candidates"]
    (parked,) = _events(root, PARKED)
    assert parked["payload"]["statement_id"] == payment["qbo_id"]
    assert _events(root, UNKNOWN) == []

    # A re-run with the card pending parks nothing new and flips nothing.
    again = _run(d, ev, bank_csv=csv, ignore_payees="Nobody")
    assert again.status == "needs_approval"
    assert len(_cards(root)) == 1

    # The owner answers (7.1): the chosen row settles on the next run,
    # attributed to the card, with the chosen row's own payment record as
    # the qbo_id (the record the auditor can verify).
    chosen = _row(root, "B-1")
    code, out = _approve(d, card["id"], capsys, row=chosen["id"])
    assert code == 0, out
    executed = _run(d, ev, bank_csv=csv)
    assert executed.status == "ok", executed.summary
    assert _row(root, "B-1")["status"] == "Paid"
    assert _row(root, "B-1")["payment_date"] == "2026-09-16"
    assert _row(root, "A-1")["status"] == "Scheduled"
    (paid,) = _events(root, PAID)
    assert paid["payload"]["invoice_id"] == chosen["id"]
    assert paid["payload"]["qbo_id"] == "BillPayment:BP2"
    assert paid["payload"]["evidence"] == "statement"
    assert paid["payload"]["statement_id"] == payment["qbo_id"]
    assert paid["payload"]["review_card"] == card["id"]
    (row_paid,) = _events(root, ROW_PAID)
    assert row_paid["payload"]["invoice_id"] == chosen["id"]
    assert row_paid["payload"]["review_card"] == card["id"]


def test_absent_param_or_missing_file_skips_the_statement_tier_silently(tmp_path):
    d, root = _engine_check(tmp_path)
    ev = _evidence_file(tmp_path)

    without = _run(d, ev)
    assert without.status == "ok"
    assert without.anomalies == []

    missing = _run(d, ev, bank_csv=tmp_path / "nowhere.csv")
    assert missing.status == "ok"
    assert missing.anomalies == []
    assert any("statement" in a and "skipped" in a for a in missing.actions), missing.actions
    assert _row(root, "N-1")["status"] == "Scheduled"
    assert _events(root, UNKNOWN) == []


def test_second_run_writes_nothing_and_the_key_declares_the_file_by_content(tmp_path):
    d, root = _engine_check(tmp_path)
    ev = _evidence_file(tmp_path)
    csv = _csv(tmp_path, ("2026-09-16", "CHECK 9058", "9058", "-125.00"))

    first = _run(d, ev, bank_csv=csv)
    assert first.status == "ok"
    assert _row(root, "N-1")["status"] == "Paid"
    history = _history_count(root)
    paid = len(_events(root, PAID))

    # The rows flipped, so the key moved: the second run EXECUTES and must
    # recognize its own settle as already recorded, writing nothing.
    second = _run(d, ev, bank_csv=csv)
    assert second.status == "ok"
    assert second.anomalies == []
    assert _history_count(root) == history
    assert len(_events(root, PAID)) == paid

    # Same inputs again: a replay. Re-touching the file changes no byte.
    third = _run(d, ev, bank_csv=csv)
    assert third.status == "noop"
    os.utime(csv, None)
    touched = _run(d, ev, bank_csv=csv)
    assert touched.status == "noop"

    # A changed statement re-executes; the settled check is remembered and
    # only the new line is news.
    csv.write_text(
        HEADER + "2026-09-16,CHECK 9058,9058,-125.00\n2026-09-20,CHECK 7777,7777,-555.00\n"
    )
    fourth = _run(d, ev, bank_csv=csv)
    assert fourth.status == "ok"
    assert [a.code for a in fourth.anomalies] == ["ap.reconcile.unknown_payment"]
    assert len(_events(root, PAID)) == paid
    assert _history_count(root) == history


# ---- boundaries --------------------------------------------------------------


def test_lines_without_a_check_number_are_out_of_scope(tmp_path):
    """ACH, card, and deposit lines carry bank text, not a payee the ledger
    knows, and no instrument the engine records: the bank rules and the
    expenses lane own them. Never unknown money here."""
    d, root = _engine_check(tmp_path)
    csv = _csv(
        tmp_path,
        ("2026-09-16", "ACH PAYMENT PAYROLL CO", "", "-7544.85"),
        ("2026-09-17", "DEPOSIT", "", "2500.00"),
        ("2026-09-17", "CARD PURCHASE 4874", "", "-125.00"),  # same amount as the check
    )

    result = _run(d, _evidence_file(tmp_path), bank_csv=csv)

    assert result.status == "ok"
    assert result.anomalies == []
    assert _events(root, UNKNOWN) == []
    assert _row(root, "N-1")["status"] == "Scheduled"


def test_a_check_qbo_already_flagged_unknown_is_not_flagged_twice(tmp_path):
    """The same physical check clears in QBO (the feed) and on the statement.
    One unknown, whichever source saw it first; the owner mutes it once."""
    d, root = _engine_check(tmp_path)
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:207",
            "txn_type": "Purchase",
            "payee": "",
            "amount_cents": 55500,
            "date": "2026-09-15",
            "check_ref": "7777",
        },
    )
    first = _run(d, ev)
    assert [a.code for a in first.anomalies] == ["ap.reconcile.unknown_payment"]
    assert len(_events(root, UNKNOWN)) == 1

    csv = _csv(tmp_path, ("2026-09-16", "CHECK 7777", "7777", "-555.00"))
    second = _run(d, ev, bank_csv=csv)

    assert second.status == "ok"
    assert second.anomalies == []
    assert len(_events(root, UNKNOWN)) == 1


def test_a_line_outside_the_date_window_parks_review_not_settle(tmp_path):
    """Reference and amount agree but the clearing is months from the
    recorded payment date: probably an earlier obligation reusing a number.
    A human decides; nothing flips, nothing is unknown."""
    d, root = _engine_check(tmp_path)
    csv = _csv(tmp_path, ("2026-12-30", "CHECK 9058", "9058", "-125.00"))

    result = _run(d, _evidence_file(tmp_path), bank_csv=csv)

    assert result.status == "needs_approval"
    (card,) = _cards(root, "pending")
    assert card["params"]["payments"][0]["source"] == "statement"
    assert _row(root, "N-1")["status"] == "Scheduled"
    assert _row(root, "N-2")["status"] == "Scheduled"
    assert _events(root, UNKNOWN) == []


def test_qbo_settled_check_makes_the_statement_line_already_recorded(tmp_path):
    """QBO evidence (an owner-entered check) settles a row first; the same
    check on the statement afterwards is already explained: no second
    event, no card, no unknown."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "G-1", 23000, "Scheduled", "9051", "", ""))
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:201",
            "txn_type": "Purchase",
            "payee": VENDOR,
            "amount_cents": 23000,
            "date": "2026-09-14",
            "check_ref": "9051",
        },
    )
    settled = _run(d, ev)
    assert settled.status == "ok"
    assert _row(root, "G-1")["status"] == "Paid"

    csv = _csv(tmp_path, ("2026-09-14", "CHECK 9051", "9051", "-230.00"))
    again = _run(d, ev, bank_csv=csv)

    assert again.status == "ok"
    assert again.anomalies == []
    assert len(_events(root, PAID)) == 1
    assert _cards(root) == []
    assert _events(root, UNKNOWN) == []


def test_shadow_reports_the_statement_settle_and_writes_nothing(tmp_path):
    d, root = _engine_check(tmp_path)
    csv = _csv(tmp_path, ("2026-09-16", "CHECK 9058", "9058", "-125.00"))

    result = _run(d, _evidence_file(tmp_path), bank_csv=csv, shadow=True)

    assert result.status == "ok"
    assert any("would settle" in a and "statement" in a for a in result.actions), result.actions
    assert _row(root, "N-1")["status"] == "Scheduled"
    assert _events(root, PAID) == []
    assert _events(root, ROW_PAID) == []
