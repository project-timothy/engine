"""Unit tests: approval queue round-trip via the CLI (DoD item 4)."""

from __future__ import annotations

from pathlib import Path

from core.engine.cli import main

LANDING = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"


def _seed(tmp_path):
    code = main(
        [
            "run",
            "demo",
            "ap",
            "intake",
            "--shadow",
            "--ledger-dir",
            str(tmp_path),
            "--param",
            f"landing_dir={LANDING}",
            "--param",
            "extractor=fixture",
        ]
    )
    assert code == 0


def test_queue_list_shows_pending_items(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    code = main(["queue", "list", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "[pending]" in out
    assert "ap.review_needs_ocr" in out
    assert "ap.new_vendor_decision" in out


def test_approve_and_reject_round_trip(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    first_id = int(out.splitlines()[0].split()[0].lstrip("#"))

    code = main(["queue", "approve", "demo", "--id", str(first_id), "--ledger-dir", str(tmp_path)])
    assert code == 0
    assert "approved" in capsys.readouterr().out

    # Approving the same item twice is an error, not a silent re-approve.
    code = main(["queue", "approve", "demo", "--id", str(first_id), "--ledger-dir", str(tmp_path)])
    assert code == 2

    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path), "--status", "approved"])
    out = capsys.readouterr().out
    assert f"#{first_id} [approved]" in out


def test_reject_records_rejected(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    some_id = int(out.splitlines()[-1].split()[0].lstrip("#"))
    code = main(["queue", "reject", "demo", "--id", str(some_id), "--ledger-dir", str(tmp_path)])
    assert code == 0
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path), "--status", "rejected"])
    assert f"#{some_id} [rejected]" in capsys.readouterr().out


def test_unknown_id_is_a_clean_error(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    code = main(["queue", "approve", "demo", "--id", "9999", "--ledger-dir", str(tmp_path)])
    assert code == 2
    assert "no approval" in capsys.readouterr().err


def test_approve_with_param_overrides_merges_into_card_params(tmp_path, capsys):
    """Issue #115 gap 2: the owner corrects a card's facts at approval time
    (the reimbursement went by check 3050, not the tenant-default Zelle).
    Overrides merge into the stored params so every downstream consumer of
    the approved card reads the corrected values."""
    _seed(tmp_path)
    capsys.readouterr()
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    first_id = int(out.splitlines()[0].split()[0].lstrip("#"))

    code = main(
        [
            "queue",
            "approve",
            "demo",
            "--id",
            str(first_id),
            "--ledger-dir",
            str(tmp_path),
            "--param",
            "channel=Check",
            "--param",
            "instrument_ref=3050",
        ]
    )
    assert code == 0

    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        (card,) = [
            c for c in ledger.list_approvals("demo", status="approved") if c["id"] == first_id
        ]
    assert card["params"]["channel"] == "Check"
    assert card["params"]["instrument_ref"] == "3050"


def test_approve_with_malformed_param_is_a_clean_error(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    first_id = int(out.splitlines()[0].split()[0].lstrip("#"))

    code = main(
        [
            "queue",
            "approve",
            "demo",
            "--id",
            str(first_id),
            "--ledger-dir",
            str(tmp_path),
            "--param",
            "not-a-pair",
        ]
    )
    assert code == 2

    # The card is untouched: still pending, params unchanged.
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path), "--status", "pending"])
    assert f"#{first_id} [pending]" in capsys.readouterr().out
