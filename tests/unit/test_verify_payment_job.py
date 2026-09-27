"""Unit tests: the verify-payment job end to end with CSV + snapshot inputs."""

from __future__ import annotations

import json
from pathlib import Path

from core.engine.runner import run

LANDING = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"


def _seed(tmp_path):
    return run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(LANDING), "extractor": "fixture"},
        ledger_dir=tmp_path,
    )


def test_verify_without_snapshot_warns_and_recommends_nothing(tmp_path):
    _seed(tmp_path)
    result = run("demo", "ap", "verify-payment", shadow=True, ledger_dir=tmp_path)
    codes = {a.code for a in result.anomalies}
    assert "ap.billpay_snapshot_missing" in codes
    # Without the queue snapshot no payment recommendation may be made.
    assert not [a for a in result.approvals_needed if a.action_type == "ap.payment_recommendation"]


def test_verify_with_full_sources_classifies_and_recommends(tmp_path):
    _seed(tmp_path)
    bank = tmp_path / "bank.csv"
    bank.write_text(
        "Posted,Memo,Chk,Value\n2026-06-05,CHECK CLEARED,501,-450.00\n",
        encoding="utf-8",
    )
    billpay = tmp_path / "billpay.json"
    billpay.write_text(
        json.dumps([{"payee": "Alpha Parts", "invoice_ref": "1002", "amount": "5200.00"}]),
        encoding="utf-8",
    )
    # Mark invoice 1001 with the cleared check's ref so the bank line joins it.
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        ledger.conn.execute(
            "UPDATE ap_invoices SET check_ref = 'CK 501' WHERE invoice_number = '1001'"
        )
        ledger.conn.commit()

    result = run(
        "demo",
        "ap",
        "verify-payment",
        shadow=True,
        params={"bank_csv": str(bank), "billpay": str(billpay)},
        ledger_dir=tmp_path,
    )
    # 1001 cleared (paid), 1002 queued (committed): zero payable, zero
    # recommendations, and the status-lag detector fires for both rows since
    # the ledger still reads Received.
    assert "0 payable" in result.summary
    assert not [a for a in result.approvals_needed if a.action_type == "ap.payment_recommendation"]
    lag = [a for a in result.anomalies if a.code == "ap.status_lag"]
    assert len(lag) == 2


def test_live_consumers_see_the_book_regardless_of_shadow_tag(tmp_path):
    """Regression for the shadow-flag silent-empty trap.

    Intake records under --shadow, so every real row is tagged shadow=1; those
    rows are the book of record once the engine files and renders them. A live
    (no --shadow) verify-payment / queue-status must therefore see the same
    book, never read it as empty. Before the fix these two consumers filtered
    shadow=0 and reported nothing on a non-empty ledger.
    """
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    _seed(tmp_path)  # shadow=True intake
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        n = ledger.conn.execute("SELECT COUNT(*) FROM ap_invoices").fetchone()[0]
    assert n > 0, "seed must record at least one invoice for this test to mean anything"

    # verify-payment run LIVE must verify the whole book, not zero rows.
    verify = run("demo", "ap", "verify-payment", shadow=False, ledger_dir=tmp_path)
    assert f"verified {n} ledger row(s)" in " ".join(verify.actions)
    assert "0 payable" not in verify.summary  # fresh rows are Received -> payable

    # queue-status run LIVE must count the book's statuses, not "none".
    queue = run("demo", "ap", "queue-status", shadow=False, ledger_dir=tmp_path)
    assert "invoice statuses: none" not in queue.summary


def test_verify_recommends_only_three_way_clean_items(tmp_path):
    _seed(tmp_path)
    bank = tmp_path / "bank.csv"
    bank.write_text("Posted,Memo,Chk,Value\n", encoding="utf-8")
    billpay = tmp_path / "billpay.json"
    billpay.write_text(
        json.dumps([{"payee": "Alpha Parts", "invoice_ref": "1002", "amount": "5200.00"}]),
        encoding="utf-8",
    )
    result = run(
        "demo",
        "ap",
        "verify-payment",
        shadow=True,
        params={"bank_csv": str(bank), "billpay": str(billpay)},
        ledger_dir=tmp_path,
    )
    recs = [a for a in result.approvals_needed if a.action_type == "ap.payment_recommendation"]
    # 1002 is committed in the queue; only 1001 is three-way payable.
    assert len(recs) == 1
    assert recs[0].params["invoice_ref"] == "1001"
