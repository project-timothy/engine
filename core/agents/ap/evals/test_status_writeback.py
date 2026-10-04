"""Half B of piece 2: the owner status write-back command.

`engine status <tenant> <ref> --scheduled|--paid|--set` flips an invoice's
status in the ledger through the transition-validated update_status, so the
forward-only rule (the 2026-05-21 ~$38K near-miss protection, invariant 4)
guards the owner's edits too. The status drives the workbook colors and clears
the parity diff's status lags.
"""

from __future__ import annotations

from core.agents.ap import store
from core.engine.cli import main
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger


def _seed(ledger_dir, *invoices):
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, status in invoices:
            store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=10000,
                status=status,
                shadow=True,
            )
    return root


def _status_of(root, number, vendor=None):
    with Ledger.open(root) as ledger:
        rows = store.invoices_by_number(ledger, "demo", number, vendor=vendor)
    return rows[0]["status"] if rows else None


def test_invoices_by_number_finds_and_disambiguates(tmp_path):
    root = _seed(
        tmp_path / "d",
        ("Acme", "1", "Received"),
        ("Beta", "1", "Received"),
        ("Gamma", "9", "Received"),
    )
    with Ledger.open(root) as ledger:
        assert len(store.invoices_by_number(ledger, "demo", "1")) == 2  # repeats across vendors
        assert len(store.invoices_by_number(ledger, "demo", "1", vendor="Acme")) == 1
        assert store.invoices_by_number(ledger, "demo", "404") == []


def test_status_command_marks_scheduled(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, ("Acme", "1", "Received"))
    assert main(["status", "demo", "1", "--scheduled", "--ledger-dir", str(d)]) == 0
    assert _status_of(root, "1") == "Scheduled"


def test_status_command_rejects_an_invalid_transition(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, ("Acme", "1", "Scheduled"))  # committed money
    rc = main(["status", "demo", "1", "--set", "Received", "--ledger-dir", str(d)])
    assert rc == 2  # cannot revert committed money
    assert _status_of(root, "1") == "Scheduled"  # unchanged


def test_status_command_errors_on_ambiguous_or_missing(tmp_path):
    d = tmp_path / "d"
    _seed(d, ("Acme", "1", "Received"), ("Beta", "1", "Received"))
    assert main(["status", "demo", "1", "--paid", "--ledger-dir", str(d)]) == 2  # ambiguous
    assert main(["status", "demo", "404", "--paid", "--ledger-dir", str(d)]) == 2  # missing


def test_status_command_reports_no_change_on_a_repeated_transition(tmp_path):
    # Payable-group oscillation is legal transition-wise (Received <-> Approved),
    # but the append-only history keys a flip by from->to, so recording the same
    # transition a second time cannot move the row again. The command must report
    # that honestly (non-zero) instead of printing a false success.
    d = tmp_path / "d"
    root = _seed(d, ("Acme", "1", "Received"))
    assert main(["status", "demo", "1", "--set", "Approved", "--ledger-dir", str(d)]) == 0
    assert main(["status", "demo", "1", "--set", "Received", "--ledger-dir", str(d)]) == 0
    # Received -> Approved is already on record: no change, surfaced as an error.
    assert main(["status", "demo", "1", "--set", "Approved", "--ledger-dir", str(d)]) == 2
    assert _status_of(root, "1") == "Received"  # unchanged by the failed re-flip
