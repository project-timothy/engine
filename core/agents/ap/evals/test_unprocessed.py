"""Unprocessed pile: files the engine could not turn into an invoice.

unresolved_unprocessed lists flagged (extraction failed) and needs_ocr
(image-only) files, minus any that have been dismissed or identified (recorded
as an invoice with that md5). A resolved file never resurfaces (2026-06-26).
"""

from __future__ import annotations

from core.agents.ap import store
from core.agents.ap.unprocessed import md5_for_file, unresolved_unprocessed
from core.ledger import Ledger
from core.ledger.event_log import append_event_line


def _event(root, key, event_type, file):
    append_event_line(
        root, {"idempotency_key": key, "event_type": event_type, "payload": {"file": file}}
    )


def test_unresolved_lists_flagged_and_needs_ocr(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        _event(ledger.root, "flag:" + "a" * 32, "ap.intake.flagged", "bad.pdf")
        _event(ledger.root, "ocr:" + "b" * 32, "ap.intake.needs_ocr", "graphic.png")
        items = unresolved_unprocessed(ledger, "t")
    assert {i["file"] for i in items} == {"bad.pdf", "graphic.png"}
    assert {i["reason"] for i in items} == {"extraction failed", "image-only, needs OCR"}


def test_dismissed_and_identified_drop_out(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        _event(ledger.root, "flag:" + "a" * 32, "ap.intake.flagged", "still_bad.pdf")
        _event(ledger.root, "ocr:" + "b" * 32, "ap.intake.needs_ocr", "junk.png")
        _event(ledger.root, "flag:" + "c" * 32, "ap.intake.flagged", "missed_invoice.pdf")
        # dismiss the junk
        _event(ledger.root, "dismiss:" + "b" * 32, "ap.intake.dismissed", "junk.png")
        # identify the missed one: recorded as an invoice with that md5
        store.insert_invoice(
            ledger,
            tenant="t",
            vendor="V",
            invoice_number="1",
            amount_cents=100,
            source_md5="c" * 32,
            shadow=True,
        )
        items = unresolved_unprocessed(ledger, "t")
    assert {i["file"] for i in items} == {"still_bad.pdf"}  # only the unresolved one


def test_md5_for_file_resolves_by_name(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        _event(ledger.root, "flag:" + "a" * 32, "ap.intake.flagged", "bad.pdf")
        assert md5_for_file(ledger, "bad.pdf") == "a" * 32
        assert md5_for_file(ledger, "not-here.pdf") is None


def test_unknown_vendor_and_incomplete_are_unprocessed_too(tmp_path):
    """2026-07-10: an unknown-vendor or incomplete-extraction file is exactly
    'a file the engine could not turn into an invoice', so it belongs to the
    pile and is resolvable by dismiss/identify. Before this, an expense report
    flagged as unknown-vendor had NO disposition path: it re-flagged and
    re-queued an approval on every intake run, forever."""
    with Ledger.open(tmp_path) as ledger:
        _event(ledger.root, "vendor:" + "d" * 32, "ap.intake.unknown_vendor", "expense.pdf")
        _event(ledger.root, "incomplete:" + "e" * 32, "ap.intake.incomplete", "partial.pdf")
        items = unresolved_unprocessed(ledger, "t")
        assert {i["file"] for i in items} == {"expense.pdf", "partial.pdf"}
        assert md5_for_file(ledger, "expense.pdf") == "d" * 32
        # dismissing resolves it for good
        _event(ledger.root, "dismiss:" + "d" * 32, "ap.intake.dismissed", "expense.pdf")
        items = unresolved_unprocessed(ledger, "t")
        assert {i["file"] for i in items} == {"partial.pdf"}
