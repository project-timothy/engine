"""The expenses agent intake + extract evals (docs/expenses-design.md, approved 2026-08-04).

Contract under test:
- Attribution is folder, then filename tag, then a card. No guess path.
- A re-dropped (renamed) duplicate noops on its content hash with a card note.
- Deterministic cross-checks flag filename/proposal disagreement, never pick.
- Meals is the default meal category; Entertainment exists only via the
  owner's explicit filename tag.
- Reference material never becomes a transaction.
"""

from __future__ import annotations

import json

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

PERSON = "Pat Owner"


def _drop(tmp_path, name: str, body: bytes = b"receipt-bytes", project: str | None = "P26_2001"):
    person_dir = tmp_path / "drop" / PERSON
    target = person_dir / project if project else person_dir
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(body)
    return target / name


def _sidecar(path, **fields):
    doc = {"doc_type": "receipt", "confidence": 0.9, **fields}
    path.with_name(path.name + ".extract.json").write_text(json.dumps(doc))


def _params(tmp_path, **extra):
    return {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "month": "2026-08",
        "extractor": "fixture",
        **extra,
    }


def _run(job, tmp_path, **extra):
    return run(
        "demo",
        "expenses",
        job,
        params=_params(tmp_path, **extra),
        ledger_dir=tmp_path / "ledger",
    )


def _events(tmp_path, event_type):
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def test_intake_files_by_folder_attribution(tmp_path):
    _drop(tmp_path, "lunch.pdf")

    result = _run("intake", tmp_path)

    assert result.status == "ok"
    landed = _events(tmp_path, "expense.receipt_landed")
    assert len(landed) == 1
    assert landed[0]["payload"]["project"] == "P26_2001"
    filed = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001" / "lunch.pdf"
    assert filed.is_file()


def test_filename_tag_is_the_fallback_attribution(tmp_path):
    _drop(tmp_path, "hotel P26_2002.pdf", project=None)

    _run("intake", tmp_path)

    landed = _events(tmp_path, "expense.receipt_landed")
    assert landed[0]["payload"]["project"] == "P26_2002"


def test_no_attribution_cards_and_never_guesses(tmp_path):
    _drop(tmp_path, "mystery.pdf", project=None)

    result = _run("intake", tmp_path)

    assert result.status == "needs_approval"
    (card,) = result.approvals_needed
    assert card.action_type == "expenses.attribute_receipt"
    assert _events(tmp_path, "expense.receipt_landed") == []
    assert not (tmp_path / "filing").exists()


def test_renamed_redrop_noops_on_the_hash_with_a_card_note(tmp_path):
    original = _drop(tmp_path, "scan.pdf", body=b"identical-bytes")
    _run("intake", tmp_path)
    original.rename(original.with_name("scan-again.pdf"))

    result = _run("intake", tmp_path)

    assert len(_events(tmp_path, "expense.receipt_landed")) == 1
    (card,) = result.approvals_needed
    assert card.action_type == "expenses.duplicate_drop"


def test_extract_flags_filename_amount_disagreement(tmp_path):
    path = _drop(tmp_path, "dinner $43.87.pdf")
    _sidecar(path, vendor_name="Bistro", amount="41.00", invoice_date="2026-08-01")
    _run("intake", tmp_path)
    # intake copies the receipt; the sidecar must sit next to the filed copy
    filed = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    _sidecar(filed / "dinner $43.87.pdf", vendor_name="Bistro", amount="41.00")

    _run("extract", tmp_path)

    (proposal,) = _events(tmp_path, "expense.extract_proposed")
    flags = " ".join(proposal["payload"]["flags"])
    assert "disagrees" in flags
    # neither side silently wins: the proposal amount stands, the flag rides
    assert proposal["payload"]["amount_cents"] == 4100


def test_meals_default_and_entertainment_only_by_owner_tag(tmp_path):
    a = _drop(tmp_path, "steakhouse.pdf", body=b"a")
    b = _drop(tmp_path, "clientnight ENT.pdf", body=b"b")
    _run("intake", tmp_path)
    filed = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    del a, b
    _sidecar(filed / "steakhouse.pdf", amount="90.00", category="Entertainment")
    _sidecar(filed / "clientnight ENT.pdf", amount="120.00", category="Entertainment")

    _run("extract", tmp_path)

    by_file = {
        e["payload"]["file"]: e["payload"] for e in _events(tmp_path, "expense.extract_proposed")
    }
    # an LLM "Entertainment" without the owner's tag downgrades to Meals
    assert by_file["steakhouse.pdf"]["category"] == "Meals"
    # the owner's explicit filename tag is the only path to Entertainment
    assert by_file["clientnight ENT.pdf"]["category"] == "Entertainment"


def test_reference_material_is_filed_not_transacted(tmp_path):
    _drop(tmp_path, "bucket_map.png", body=b"map-bytes")
    _run("intake", tmp_path)
    filed = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    (filed / "bucket_map.png.extract.json").write_text(
        json.dumps({"doc_type": "reference", "confidence": 0.95})
    )

    _run("extract", tmp_path)

    assert _events(tmp_path, "expense.extract_proposed") == []
    assert len(_events(tmp_path, "expense.reference_detected")) == 1
    assert (tmp_path / "filing" / "2026-08" / "_reference" / "bucket_map.png").is_file()
    assert not (filed / "bucket_map.png").exists()


def test_unverified_copy_is_not_filed_and_reference_original_stays_put(tmp_path, monkeypatch):
    """Honesty audit 2026-09-03, 03-F4 (S2). "filed" used to stand on the copy
    call returning; the reference branch then deleted the only copy. Now the
    destination is read back: a short write is an anomaly, no landed event,
    and the original is never unlinked."""
    import shutil
    from pathlib import Path

    from core.engine import fileops

    real_copy2 = shutil.copy2

    def _short_copy(src, dst):
        Path(dst).write_bytes(b"trunc")  # a truncated write on a sync mount

    monkeypatch.setattr(fileops.shutil, "copy2", _short_copy)
    _drop(tmp_path, "receipt.pdf", body=b"receipt-bytes")

    result = _run("intake", tmp_path)

    assert any(a.code == "expenses.copy_unverified" for a in result.anomalies)
    assert _events(tmp_path, "expense.receipt_landed") == []
    filed_dir = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    assert not (filed_dir / "receipt.pdf").exists()  # the partial copy was removed
    assert (tmp_path / "drop" / PERSON / "P26_2001" / "receipt.pdf").exists()

    # The reference branch on a good intake, then a bad move: the original stays.
    monkeypatch.setattr(fileops.shutil, "copy2", real_copy2)
    _drop(tmp_path, "bucket_map.png", body=b"map-bytes")
    _run("intake", tmp_path)
    (filed_dir / "bucket_map.png.extract.json").write_text(
        json.dumps({"doc_type": "reference", "confidence": 0.95})
    )
    monkeypatch.setattr(fileops.shutil, "copy2", _short_copy)

    _run("extract", tmp_path)

    assert (filed_dir / "bucket_map.png").exists()  # never unlinked on an unverified copy
    assert not (tmp_path / "filing" / "2026-08" / "_reference" / "bucket_map.png").exists()
    assert _events(tmp_path, "expense.reference_detected") == []


# ---- honesty audit 2026-09-03: counts and shadow claims ---------------------


def test_already_filed_and_duplicate_are_counted_apart(tmp_path):
    """03-F15 (S4). "duplicate N" used to count every already-filed receipt
    still sitting in the drop tree, every run, so the owner read a duplicate
    count that was really "already filed". The two are now separate: a file
    left in the drop after a good filing is already-filed; only the renamed
    re-drop (the 2026-05-18 shape, with its card note) is a duplicate."""
    original = _drop(tmp_path, "scan.pdf", body=b"identical-bytes")
    first = _run("intake", tmp_path)
    assert "filed 1, already-filed 0, duplicate 0" in first.summary

    _drop(tmp_path, "lunch.pdf", body=b"lunch-bytes")
    second = _run("intake", tmp_path)
    assert "filed 1, already-filed 1, duplicate 0" in second.summary
    assert second.approvals_needed == []

    original.rename(original.with_name("scan-again.pdf"))
    third = _run("intake", tmp_path)
    assert "filed 0, already-filed 1, duplicate 1" in third.summary
    (card,) = third.approvals_needed
    assert card.action_type == "expenses.duplicate_drop"


def test_shadow_extract_files_no_reference_and_records_no_event(tmp_path):
    """03-F8 (S3). A shadow extract used to record expense.reference_detected
    with a filed_to that was never written; the real runs then excluded the
    sha everywhere ("nothing pending", "nothing unconsumed") while the
    receipt sat loose forever. Shadow now emits a "would file" action and no
    event, and the following real run files it."""
    _drop(tmp_path, "bucket_map.png", body=b"map-bytes")
    _run("intake", tmp_path)
    filed = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    (filed / "bucket_map.png.extract.json").write_text(
        json.dumps({"doc_type": "reference", "confidence": 0.95})
    )

    shadow = run(
        "demo",
        "expenses",
        "extract",
        params=_params(tmp_path),
        ledger_dir=tmp_path / "ledger",
        shadow=True,
    )

    assert shadow.status == "ok"
    assert any("would file" in a and "_reference/" in a for a in shadow.actions)
    assert "reference 0" in shadow.summary
    assert _events(tmp_path, "expense.reference_detected") == []
    assert (filed / "bucket_map.png").is_file()
    assert not (tmp_path / "filing" / "2026-08" / "_reference").exists()

    real = _run("extract", tmp_path)

    assert real.status == "ok"  # the shadow run left nothing behind to skip
    assert len(_events(tmp_path, "expense.reference_detected")) == 1
    assert (tmp_path / "filing" / "2026-08" / "_reference" / "bucket_map.png").is_file()
