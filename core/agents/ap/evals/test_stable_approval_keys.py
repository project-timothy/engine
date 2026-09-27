"""Approval cards are deduped by stable subject, not re-parked per run.

The 2026-08-20 assessment probe: approval idempotency keys were namespaced
under the per-run key, so any landing change re-fired intake and parked a
brand-new pending card for every still-flagged file (unknown vendor,
needs-OCR, REVISED), and re-appended the identical REVISED note to the
ledger row. Live evidence: the production queue held the same inline
graphic parked twice (cards #2 and #16). The recorded decision is the memory —
a resolved card must not resurrect for the same subject, and a genuinely
new subject (new content hash) still parks exactly once.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import minimal_pdf
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

LANDING = Path(__file__).resolve().parent / "fixtures" / "landing"


def _intake(tmp_path, landing):
    return run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=tmp_path,
    )


def _pending_by_subject(root):
    with Ledger.open(root) as ledger:
        pending = ledger.list_approvals("demo", status="pending")
    by: dict[tuple[str, str], int] = {}
    for p in pending:
        k = (p["action_type"], p["params"].get("file", ""))
        by[k] = by.get(k, 0) + 1
    return by


def test_landing_change_re_parks_nothing_for_still_flagged_files(tmp_path):
    landing = tmp_path / "landing"
    shutil.copytree(LANDING, landing)
    _intake(tmp_path, landing)

    # Next morning: one new flagged file lands; everything else unchanged.
    # (Distinct content -> distinct md5 -> a genuinely new subject.)
    (landing / "brand-new-scan.pdf").write_bytes(minimal_pdf("new day"))
    (landing / "brand-new-scan.pdf.extract.json").write_text(
        '{"doc_type": "unknown", "confidence": 0.0, "needs_ocr": true}'
    )
    _intake(tmp_path, landing)

    root = resolve_ledger_root("demo", tmp_path)
    by = _pending_by_subject(root)
    dupes = {k: v for k, v in by.items() if v > 1}
    assert dupes == {}, f"duplicate pending cards: {dupes}"
    # The new subject's own card parked exactly once.
    assert by[("ap.review_needs_ocr", "brand-new-scan.pdf")] == 1


def test_revised_note_is_appended_once_not_per_run(tmp_path):
    landing = tmp_path / "landing"
    shutil.copytree(LANDING, landing)
    _intake(tmp_path, landing)
    (landing / "brand-new-scan.pdf").write_bytes(minimal_pdf("new day"))
    _intake(tmp_path, landing)

    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        row = ledger.conn.execute(
            "SELECT notes FROM ap_invoices WHERE invoice_number='1001'"
        ).fetchone()
    assert row["notes"].count("Pending REVISED review") == 1


def test_resolved_card_never_resurrects_for_the_same_subject(tmp_path):
    landing = tmp_path / "landing"
    shutil.copytree(LANDING, landing)
    _intake(tmp_path, landing)

    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        (card,) = [
            c
            for c in ledger.list_approvals("demo", status="pending")
            if c["action_type"] == "ap.review_revised_invoice"
        ]
        ledger.resolve_approval("demo", card["id"], "rejected")

    (landing / "brand-new-scan.pdf").write_bytes(minimal_pdf("new day"))
    _intake(tmp_path, landing)

    with Ledger.open(root) as ledger:
        again = [
            c
            for c in ledger.list_approvals("demo", status="pending")
            if c["action_type"] == "ap.review_revised_invoice"
        ]
    assert again == [], "the rejected decision is the memory; no resurrection"
