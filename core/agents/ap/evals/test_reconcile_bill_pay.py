"""ap/reconcile: a bank-written bill-pay check settles the row it paid
(issue #285, the narrow exception to 7.3's check-number rule).

The shape that exposed the gap: a $520.00 payable was put into the bank's
bill-pay queue on 2026-09-17 to go out on the 30th. The bank writes that
check, so its number does not exist at scheduling; it appears on the
statement at clearing. The row therefore sits Scheduled with the right
amount and the right payment date and NO reference, and the statement line
carrying the number matches nothing.

Contract under test:

- a committed row whose vendor's registry ``payment_channel`` is one the
  tenant names as bank-written, carrying no reference, whose amount equals
  the line and whose recorded SEND date sits inside the same asymmetric
  window the rest of the tier uses (up to the deposit lag before the
  clearing, at most the entry slack after it), settles and takes the number
  from the line;
- exactly one such row, or nothing happens: two candidates park the 7.1
  review card, never a guess;
- the tier is inert until the tenant names its channels, so no tenant
  inherits it by upgrading;
- a vendor the registry pays another way, a clearing outside the window,
  and a row already carrying a different number are all untouched by it.

The demo tenant's registry supplies the channel vocabulary ("ACH" on Alpha
Parts, nothing on Alpha Fabrication) and ``--param bill_pay_channels``
names it, so no eval reads a real tenant's configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Alpha Parts"  # demo registry: payment_channel = "ACH"
OTHER = "Alpha Fabrication"  # demo registry: no payment_channel at all
CHANNEL = "ACH"
CARD = "ap.reconcile_review"
PAID = "ap.reconcile.paid"
UNKNOWN = "ap.reconcile.unknown"
SENT = "2026-09-30"  # the day the owner told the bank to send it
CLEARED = "2026-10-02"  # the day it came back on the statement

HEADER = "Posted,Memo,Chk,Value\n"


def _csv(tmp_path: Path, *lines, name: str = "statement.csv") -> Path:
    p = tmp_path / name
    p.write_text(HEADER + "".join(f"{d},{m},{c},{v}\n" for d, m, c, v in lines))
    return p


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status, payment_date, check_ref)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, payment_date, check_ref in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
                invoice_date="2026-09-05",
            )
            if payment_date or check_ref:
                store.record_payment_details(
                    ledger,
                    invoice_id=inv_id,
                    payment_date=payment_date,
                    check_ref=check_ref,
                )
    return root


def _run(ledger_dir: Path, tmp_path: Path, csv: Path, **extra):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps([]))
    params = {"evidence_file": str(evidence), "bank_csv": str(csv), **extra}
    return run("demo", "ap", "reconcile", params=params, ledger_dir=ledger_dir)


def _row(root, number):
    with Ledger.open(root) as ledger:
        return store.invoices_by_number(ledger, "demo", number)[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _cards(root, status=None):
    with Ledger.open(root) as ledger:
        return [c for c in ledger.list_approvals("demo", status=status) if c["action_type"] == CARD]


def test_a_bank_written_check_settles_the_scheduled_row_and_lands_its_number(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "R-1675", 52000, "Scheduled", SENT, ""))
    csv = _csv(tmp_path, (CLEARED, "CHECK 9042", "9042", "-520.00"))

    result = _run(d, tmp_path, csv, bill_pay_channels=CHANNEL)

    assert result.status == "ok", result.summary
    row = _row(root, "R-1675")
    assert row["status"] == "Paid"
    assert row["check_ref"] == "9042"  # the number the bank wrote, from the line
    assert row["payment_date"] == CLEARED
    (paid,) = _events(root, PAID)
    assert paid["payload"]["evidence"] == "statement"
    assert paid["payload"]["check_ref"] == "9042"
    assert _events(root, UNKNOWN) == []
    assert result.anomalies == []


def test_the_tier_is_inert_until_the_tenant_names_its_channels(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "R-1675", 52000, "Scheduled", SENT, ""))
    csv = _csv(tmp_path, (CLEARED, "CHECK 9042", "9042", "-520.00"))

    result = _run(d, tmp_path, csv)

    assert result.status == "ok", result.summary
    assert _row(root, "R-1675")["status"] == "Scheduled"
    assert [a.code for a in result.anomalies] == ["ap.reconcile.unknown_payment"]
    assert len(_events(root, UNKNOWN)) == 1


def test_two_candidates_at_one_amount_park_review_and_never_guess(tmp_path):
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "R-1675", 52000, "Scheduled", SENT, ""),
        (VENDOR, "R-1676", 52000, "Scheduled", SENT, ""),
    )
    csv = _csv(tmp_path, (CLEARED, "CHECK 9042", "9042", "-520.00"))

    result = _run(d, tmp_path, csv, bill_pay_channels=CHANNEL)

    assert result.status == "needs_approval", result.summary
    assert _row(root, "R-1675")["status"] == "Scheduled"
    assert _row(root, "R-1676")["status"] == "Scheduled"
    assert len(_cards(root, "pending")) == 1
    assert _events(root, UNKNOWN) == []


def test_another_channel_a_stale_date_and_a_row_with_its_own_number_are_untouched(tmp_path):
    """Three near misses, one run: the registry pays this vendor another way;
    the clearing is weeks off the recorded send date; the row already names a
    different check (a different physical payment, the #134 boundary)."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (OTHER, "F-1", 52000, "Scheduled", SENT, ""),
        (VENDOR, "R-2", 61000, "Scheduled", "2026-08-20", ""),
        (VENDOR, "R-3", 77000, "Scheduled", SENT, "1111"),
    )
    csv = _csv(
        tmp_path,
        (CLEARED, "CHECK 9042", "9042", "-520.00"),
        (CLEARED, "CHECK 9043", "9043", "-610.00"),
        (CLEARED, "CHECK 9044", "9044", "-770.00"),
    )

    result = _run(d, tmp_path, csv, bill_pay_channels=CHANNEL)

    assert result.status == "ok", result.summary
    for number in ("F-1", "R-2", "R-3"):
        assert _row(root, number)["status"] == "Scheduled"
    assert {e["payload"]["check_ref"] for e in _events(root, UNKNOWN)} == {"9042", "9043", "9044"}
    assert _events(root, PAID) == []


def test_the_window_runs_from_the_send_date_to_the_deposit(tmp_path):
    """The recorded date is the day the bank SENDS the check; the clearing is
    the day the payee deposits it, one to three weeks later. So the window is
    the tier's usual asymmetric one: late clearings are ordinary life, a
    clearing before the send date beyond entry slack is not."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "W-14", 52000, "Scheduled", "2026-09-18", ""),  # 14 days out
        (VENDOR, "W-30", 61000, "Scheduled", "2026-09-02", ""),  # 30 days out
        (VENDOR, "W-AFTER", 77000, "Scheduled", "2026-10-08", ""),  # 6 days late
    )
    csv = _csv(
        tmp_path,
        (CLEARED, "CHECK 9042", "9042", "-520.00"),
        (CLEARED, "CHECK 9043", "9043", "-610.00"),
        (CLEARED, "CHECK 9044", "9044", "-770.00"),
    )

    result = _run(d, tmp_path, csv, bill_pay_channels=CHANNEL)

    assert result.status == "ok", result.summary
    assert _row(root, "W-14")["status"] == "Paid"
    assert _row(root, "W-14")["check_ref"] == "9042"
    assert _row(root, "W-30")["status"] == "Scheduled"
    assert _row(root, "W-AFTER")["status"] == "Scheduled"
    assert {e["payload"]["check_ref"] for e in _events(root, UNKNOWN)} == {"9043", "9044"}
