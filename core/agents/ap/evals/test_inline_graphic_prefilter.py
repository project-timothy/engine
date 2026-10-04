"""Pre-filter: inline email graphics are skipped before extraction.

2026-06-25 shadow finding: inline email images (imageNNN.png, Outlook-*.png)
saved into the landing folder alongside real attachments were sent to
extraction, returned needs_ocr, and piled up in the review queue (six in one
day). They are never invoices, so a deterministic filename gate skips them
before the model call. The rule is image-only and name-shaped, so a real
invoice PDF (or a real scanned-image invoice) is never skipped.

The name the rule reads is the name the engine's own saver wrote, not the one
the sender sent: a save into a taken name gets the placement contract's
collision suffix (``core.engine.fileops``, ``{stem} (n){suffix}``), and an
anchored rule that does not know that convention stops matching the second
copy of a logo it skipped the first time.
"""

from __future__ import annotations

from core.agents.ap.jobs import _looks_like_inline_graphic


def test_looks_like_inline_graphic_classifies_email_junk():
    assert _looks_like_inline_graphic("image001.png")
    assert _looks_like_inline_graphic("image012.png")
    assert _looks_like_inline_graphic("image.png")  # bare 'image', no number
    assert _looks_like_inline_graphic("Outlook-3v2tnx45.png")
    assert _looks_like_inline_graphic("Outlook-ktlyxete.jpg")


def test_looks_like_inline_graphic_never_matches_a_real_invoice():
    assert not _looks_like_inline_graphic("Invoice_10089_from_Acme_Tooling_Company.pdf")
    assert not _looks_like_inline_graphic("Acme Co - Invoice 500124.pdf")
    assert not _looks_like_inline_graphic("0955.pdf")
    assert not _looks_like_inline_graphic("scan_acme_invoice.png")  # real scanned invoice
    assert not _looks_like_inline_graphic("imagery.png")  # 'image' must be the whole base
    assert not _looks_like_inline_graphic("scan_image.png")  # not anchored at the start


def test_a_placement_collision_suffix_is_not_part_of_the_document_name():
    """The same email logo arriving twice is the same junk both times. The
    suffix belongs to the filesystem, not to the document."""
    assert _looks_like_inline_graphic("image001 (2).png")
    assert _looks_like_inline_graphic("image (19).png")
    assert _looks_like_inline_graphic("Outlook-3v2tnx45 (2).jpg")
    assert _looks_like_inline_graphic("image001.png")  # unsuffixed, unchanged


def test_the_collision_tolerance_stays_as_narrow_as_the_rule():
    """Tolerating the suffix must not widen what the rule can claim: still
    image-only, still name-shaped, still the whole base name."""
    assert not _looks_like_inline_graphic("Acme Co - Invoice 500124 (2).pdf")
    assert not _looks_like_inline_graphic("imagery (2).png")
    assert not _looks_like_inline_graphic("scan_image (2).png")
    assert not _looks_like_inline_graphic("image001 (2) scan.png")  # suffix is last or nothing
    assert not _looks_like_inline_graphic("image001 (two).png")  # a count, not a word
    assert not _looks_like_inline_graphic("image001 (2).pdf")  # never a PDF's business


def test_the_rule_reads_the_name_the_placement_contract_writes(tmp_path):
    """Pinned against the writer rather than against a literal: the eval mints
    the collision name through the contract every saver uses, so the rule and
    the convention cannot drift apart in a later refactor."""
    from core.engine.fileops import place_bytes
    from core.engine.guard import WriteGuard

    landing = tmp_path / "landing"
    guard = WriteGuard([])

    first = place_bytes(b"logo bytes", landing / "image001.png", guard=guard, shadow=False)
    second = place_bytes(b"other logo bytes", landing / "image001.png", guard=guard, shadow=False)

    assert first.dest.name != second.dest.name  # the contract suffixed the collision
    assert _looks_like_inline_graphic(first.dest.name)
    assert _looks_like_inline_graphic(second.dest.name)


def test_the_second_copy_of_one_logo_never_reaches_the_extractor(tmp_path):
    """The live shape, 2026-07-24 and 2026-09-25: the mail lane saved a second
    copy of an email logo into a taken name, the pre-filter's anchor broke on
    the placement suffix, and the byte-shaped junk the rule exists to stop
    reached the model and came back a model-authored disposition — in the same
    run that skipped ``image001.png`` deterministically."""
    from core.engine.runner import resolve_ledger_root, run
    from core.ledger import Ledger

    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "image001.png").write_bytes(b"inline-graphic")
    (landing / "image001 (2).png").write_bytes(b"inline-graphic-again")

    run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=tmp_path / "data",
    )

    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        events = ledger.read_event_log()
    skipped = {
        e["payload"]["file"]: e["payload"].get("reason", "")
        for e in events
        if e["event_type"] == "ap.intake.skipped"
    }
    assert "image001.png" in skipped
    assert "image001 (2).png" in skipped
    assert "graphic" in skipped["image001 (2).png"]
    # No verdict about it but the deterministic one: nothing was asked.
    assert not [
        e
        for e in events
        if (e.get("payload") or {}).get("file") == "image001 (2).png"
        and e["event_type"] != "ap.intake.skipped"
    ]


def test_intake_skips_inline_graphics_but_not_the_real_invoice(tmp_path):
    from core.engine.runner import resolve_ledger_root, run
    from core.ledger import Ledger

    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "image001.png").write_bytes(b"inline-graphic")
    (landing / "Outlook-abc123.png").write_bytes(b"inline-graphic-2")
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF stub")
    (landing / "Invoice_777.pdf.extract.json").write_text(
        '{"doc_type": "invoice", "vendor_name": "Acme", "invoice_number": "777",'
        ' "amount": "10.00", "invoice_date": "2026-06-20", "confidence": 0.95}',
        encoding="utf-8",
    )

    run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=tmp_path / "data",
    )

    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        events = ledger.read_event_log()
    skipped = {
        e["payload"]["file"]: e["payload"].get("reason", "")
        for e in events
        if e["event_type"] == "ap.intake.skipped"
    }
    assert "image001.png" in skipped and "graphic" in skipped["image001.png"]
    assert "Outlook-abc123.png" in skipped
    # the real invoice was not skipped; it reached extraction and produced a
    # non-skip event of its own.
    assert "Invoice_777.pdf" not in skipped
    invoice_events = [e for e in events if e.get("payload", {}).get("file") == "Invoice_777.pdf"]
    assert any(e["event_type"] != "ap.intake.skipped" for e in invoice_events)
