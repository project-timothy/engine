"""Resolving the unprocessed pile: dismiss (not an invoice) and identify (manual).

Once an unprocessed file is dismissed or identified it never resurfaces in the
"needs identification" section. Resolution is by command (the workbook is a
regenerated view), 2026-06-26 design.
"""

from __future__ import annotations

from core.agents.ap import store
from core.agents.ap.unprocessed import unresolved_unprocessed
from core.engine.cli import main
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.ledger.event_log import append_event_line


def _seed_flagged(root, md5, file):
    append_event_line(
        root,
        {
            "idempotency_key": f"flag:{md5}",
            "event_type": "ap.intake.flagged",
            "payload": {"file": file},
        },
    )


def test_dismiss_removes_a_file_for_good(tmp_path):
    d = tmp_path / "d"
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        _seed_flagged(ledger.root, "a" * 32, "junk.png")
        assert unresolved_unprocessed(ledger, "demo")  # present before

    assert main(["dismiss", "demo", "junk.png", "--ledger-dir", str(d)]) == 0

    with Ledger.open(root) as ledger:
        assert unresolved_unprocessed(ledger, "demo") == []  # gone, will not resurface


def test_dismiss_all_clears_the_whole_pile(tmp_path):
    d = tmp_path / "d"
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        _seed_flagged(ledger.root, "a" * 32, "junk1.pdf")
        _seed_flagged(ledger.root, "b" * 32, "junk2.png")
        _seed_flagged(ledger.root, "c" * 32, "junk3.jpg")
        assert len(unresolved_unprocessed(ledger, "demo")) == 3

    assert main(["dismiss", "demo", "--all", "--ledger-dir", str(d)]) == 0

    with Ledger.open(root) as ledger:
        assert unresolved_unprocessed(ledger, "demo") == []  # all gone at once


def test_dismiss_unknown_file_errors(tmp_path):
    d = tmp_path / "d"
    with Ledger.open(resolve_ledger_root("demo", d)):
        pass  # initialize the ledger
    assert main(["dismiss", "demo", "ghost.png", "--ledger-dir", str(d)]) == 1


def test_identify_records_the_invoice_and_clears_it(tmp_path):
    d = tmp_path / "d"
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        _seed_flagged(ledger.root, "c" * 32, "missed_invoice.pdf")

    rc = main(
        [
            "identify",
            "demo",
            "missed_invoice.pdf",
            "--vendor",
            "Acme",
            "--number",
            "777",
            "--amount",
            "272.00",
            "--ledger-dir",
            str(d),
        ]
    )
    assert rc == 0

    with Ledger.open(root) as ledger:
        assert unresolved_unprocessed(ledger, "demo") == []  # identified -> drops out
        inv = store.invoices_by_number(ledger, "demo", "777")
        assert len(inv) == 1
        assert inv[0]["vendor"] == "Acme" and inv[0]["amount_cents"] == 27200
