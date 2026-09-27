"""AP agent evals: the whole intake pipe over a synthetic landing folder.

Runs through the real runner (config -> extraction boundary -> dedup ->
ledger -> approvals -> commit) using the deterministic fixture extractor.
The landing fixture deliberately mirrors the real folder's character: real
invoices mixed with a PO, a reference photo, a no-text scan, an unknown
vendor, a duplicate, a revision, and CAD-file noise.
"""

from __future__ import annotations

from pathlib import Path

from core.engine.runner import run

LANDING = Path(__file__).resolve().parent / "fixtures" / "landing"


def _intake(tmp_path, shadow=True):
    return run(
        "demo",
        "ap",
        "intake",
        shadow=shadow,
        params={"landing_dir": str(LANDING), "extractor": "fixture"},
        ledger_dir=tmp_path,
    )


def test_intake_processes_the_noisy_folder_end_to_end(tmp_path):
    result = run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(LANDING), "extractor": "fixture"},
        ledger_dir=tmp_path,
    )
    assert result.status == "needs_approval"
    s = result.summary
    assert "NEW 2" in s  # 1001 + 1002
    assert "DUPLICATE 1" in s  # the second copy of 1001
    assert "REVISED 1" in s  # the changed-amount copy
    assert "ROUTED 2" in s  # the PO and the reference photo
    assert "FLAGGED 1" in s  # the unknown-vendor invoice
    assert "NEEDS_OCR 1" in s  # the no-text scan
    assert "SKIPPED 1" in s  # the CAD file


def test_intake_queues_the_right_approvals(tmp_path):
    result = _intake(tmp_path)
    by_action: dict[str, int] = {}
    for ap in result.approvals_needed:
        by_action[ap.action_type] = by_action.get(ap.action_type, 0) + 1
    # NEW invoices auto-file now: no file-invoice approval gate (2026-06-29),
    # since filing is not a money move and junk is kept out upstream. The
    # approvals that remain are the genuine human-judgment ones.
    assert "ap.file_invoice" not in by_action
    assert by_action["ap.new_vendor_decision"] == 1
    assert by_action["ap.review_revised_invoice"] == 1
    assert by_action["ap.review_needs_ocr"] == 1


def test_intake_rerun_is_a_noop_and_never_double_posts(tmp_path):
    first = _intake(tmp_path)
    assert first.status == "needs_approval"
    second = _intake(tmp_path)
    assert second.status == "noop"

    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        count = ledger.conn.execute("SELECT COUNT(*) FROM ap_invoices").fetchone()[0]
        assert count == 2  # 1001 and 1002, exactly once each


def test_revised_amount_never_overwrites_the_recorded_row(tmp_path):
    _intake(tmp_path)
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        row = ledger.conn.execute(
            "SELECT amount_cents, notes FROM ap_invoices WHERE invoice_number = '1001'"
        ).fetchone()
        assert row["amount_cents"] == 45000  # original 450.00 kept
        assert "Pending REVISED review" in row["notes"]


def test_would_file_to_matches_the_actual_filed_name(tmp_path):
    # The recorded event's predicted filed name must equal what the apply job
    # actually files (same _filed_dest_name helper, amount formatted to cents),
    # so the ledger's prediction never drifts from reality (e.g. $450.5 vs
    # $450.50 or a vendor slash leaking into a subfolder).
    import re

    _intake(tmp_path)
    from core.agents.ap.jobs import _filed_dest_name, _filed_month
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        recorded = [
            e for e in ledger.read_event_log() if e.get("event_type") == "ap.invoice.recorded"
        ]
        rows = {
            r["invoice_number"]: dict(r)
            for r in ledger.conn.execute(
                "SELECT vendor, invoice_number, amount_cents, invoice_date FROM ap_invoices"
            ).fetchall()
        }
    assert recorded
    for event in recorded:
        payload = event["payload"]
        row = rows[payload["invoice_number"]]
        expected = _filed_month(row) + "/" + _filed_dest_name(row)
        assert payload["would_file_to"] == expected
        assert re.search(r"\$\d+\.\d{2}\.pdf$", payload["would_file_to"])  # two-decimal amount


def test_shadow_flag_is_set_on_every_row(tmp_path):
    _intake(tmp_path, shadow=True)
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        rows = ledger.conn.execute("SELECT shadow FROM ap_invoices").fetchall()
        assert rows and all(r["shadow"] == 1 for r in rows)


def test_2026_08_20_vendor_onboarding_refires_intake_on_the_same_landing_state(tmp_path):
    """Issue #135: the intake key hashed only landing state, so the natural
    "onboard the vendor, re-run" workflow replayed the FLAGGED result and
    the invoice was never recorded until unrelated mail changed the folder.
    Vendor identity (and the extractor choice) are run inputs — the
    2026-07-20 lesson the verify and push keys already carry."""
    import shutil as _shutil
    from pathlib import Path as _Path

    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    vendors = tmp_path / "vendors.toml"
    _shutil.copy(
        _Path(__file__).resolve().parents[4] / "tenants" / "demo" / "vendors.toml", vendors
    )
    params = {
        "landing_dir": str(LANDING),
        "extractor": "fixture",
        "vendors_toml": str(vendors),
    }

    first = run("demo", "ap", "intake", shadow=True, params=params, ledger_dir=tmp_path)
    assert "FLAGGED 1" in first.summary  # Mystery Co is unknown

    with vendors.open("a") as handle:
        handle.write('\n["mystery.example"]\nvendor = "Mystery Co"\ncost_type = "Materials"\n')

    second = run("demo", "ap", "intake", shadow=True, params=params, ledger_dir=tmp_path)

    assert second.status != "noop"  # the registry change re-fires the run
    assert "FLAGGED" not in second.summary  # zero counters never print
    assert "NEW 1" in second.summary  # the once-flagged invoice records
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        row = ledger.conn.execute(
            "SELECT vendor FROM ap_invoices WHERE invoice_number = '9'"
        ).fetchone()
        assert row is not None and row["vendor"] == "Mystery Co"


def test_intake_handoff_rubric_is_complete_on_the_noisy_folder(tmp_path):
    """Honesty audit 2026-09-03, 02-F9: the handoff rubric is "every counted
    disposition left an event". The noisy folder exercises every disposition
    with no W-9 lane in play (the demo tenant sets no folder), so the score
    must read 1.0 here and stay 1.0 when a W-9 execution event joins the
    list (pinned in test_w9_intake.py)."""
    result = _intake(tmp_path)
    assert result.rubric.handoff_quality == 1.0
