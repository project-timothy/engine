"""Unit tests for the filesystem sweep (landing tree vs the engine ledger).

The sweep is the inverse of the parity diff: it compares the filesystem against
the engine ledger to catch an invoice that physically exists but the engine
never booked, because it was filed where intake does not look (a subfolder, the
manual _skipped pile). 2026-06-24 shadow finding: a real D&E payable sat
unbooked in _skipped, invisible to the date-windowed, ledger-vs-ledger diff.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.agents.ap import store
from core.agents.ap.sweep import (
    SweepReport,
    _is_invoice_like,
    collect_unbooked,
    render_markdown,
    seen_fingerprints,
)
from core.ledger import Ledger
from core.ledger.event_log import append_event_line


def _write(path: Path, data: bytes = b"%PDF-1.4 stub") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.md5(data).hexdigest()


def test_is_invoice_like_recognizes_invoices_and_skips_graphics():
    assert _is_invoice_like("Invoice_20053_from_DE.pdf", ".pdf")
    assert _is_invoice_like("Harbor Signs - Invoice 500123.pdf", ".pdf")
    assert _is_invoice_like("0955.pdf", ".pdf")  # number-only PDF is invoice-like
    assert not _is_invoice_like("companylogo.png", ".png")
    assert not _is_invoice_like("image003.png", ".png")  # inline email graphic
    assert not _is_invoice_like("notes.pdf", ".pdf")  # no 'invoice', no number


def test_collect_unbooked_surfaces_an_invoice_misfiled_in_a_subfolder(tmp_path):
    # Top level is intake's windowed domain: an unbooked file there is not a
    # sweep finding. A booked file in a subfolder is excluded by its
    # fingerprint. An unbooked invoice in a subfolder is the finding.
    _write(tmp_path / "Invoice_toplevel.pdf", b"top-unbooked")
    booked = _write(tmp_path / "_archive" / "Invoice_old.pdf", b"already-booked")
    _write(tmp_path / "_skipped" / "Invoice_20053_from_DE.pdf", b"unbooked-payable")
    report = collect_unbooked(tmp_path, seen={booked})
    assert report.unbooked_invoices == ["_skipped/Invoice_20053_from_DE.pdf"]
    assert report.scanned == 2  # two subfolder files; the top-level one is skipped
    assert not report.is_clean


def test_collect_unbooked_honors_operator_marked_non_invoice_folders(tmp_path):
    # The operator marks a misrouted pile as non-invoices; the sweep honors that
    # label and never re-flags its contents, while still scanning _skipped itself
    # (the convention must not re-hide a real invoice the way _skipped did 20053).
    _write(tmp_path / "_skipped" / "Invoice_20053.pdf", b"real-invoice")
    _write(tmp_path / "_skipped" / "non-invoice-misrouted-2026-06-09" / "PO260205.pdf", b"a-po")
    _write(tmp_path / "_skipped" / "misrouted" / "Proposal_P26_2023.pdf", b"a-proposal")
    report = collect_unbooked(tmp_path, seen=set())
    assert report.unbooked_invoices == ["_skipped/Invoice_20053.pdf"]
    assert report.scanned == 1  # only the file outside the marked folders


def test_top_level_files_are_intake_domain_not_sweep_findings(tmp_path):
    # The sweep deliberately ignores the top level (the pre-window backlog there
    # is intake's, not a misfiling). Only subfolders are the structural blind spot.
    _write(tmp_path / "Invoice_unbooked_toplevel.pdf", b"top")
    report = collect_unbooked(tmp_path, seen=set())
    assert report.scanned == 0
    assert report.is_clean


def test_collect_unbooked_stays_quiet_on_non_invoice_junk(tmp_path):
    _write(tmp_path / "_skipped" / "companylogo.png", b"logo-bytes")
    report = collect_unbooked(tmp_path, seen=set())
    assert report.unbooked_invoices == []
    assert report.other_unbooked == 1
    assert report.is_clean  # junk does not raise the verdict


def test_collect_unbooked_ignores_files_the_engine_already_booked(tmp_path):
    md5 = _write(tmp_path / "_skipped" / "Invoice_99.pdf", b"already-seen")
    report = collect_unbooked(tmp_path, seen={md5})
    assert report.is_clean
    assert not report.unbooked_invoices


def test_seen_fingerprints_reads_recorded_invoices_and_event_keys(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        store.insert_invoice(
            ledger,
            tenant="t",
            vendor="V",
            invoice_number="1",
            amount_cents=100,
            source_md5="a" * 32,
            shadow=True,
        )
        append_event_line(ledger.root, {"idempotency_key": "flag:" + "b" * 32})
        append_event_line(ledger.root, {"idempotency_key": "skip:logo.png"})
        seen = seen_fingerprints(ledger, "t")
    assert "a" * 32 in seen  # recorded invoice fingerprint
    assert "b" * 32 in seen  # flagged file fingerprint, parsed from the event key
    assert all(len(s) == 32 for s in seen)  # skip:<name> is not a fingerprint


def test_render_lists_unbooked_invoices_and_reads_needs_attention(tmp_path):
    _write(tmp_path / "_skipped" / "Invoice_20053.pdf", b"x")
    report = collect_unbooked(tmp_path, seen=set())
    rendered = render_markdown(report, tenant="demo")
    assert "Unbooked invoice-like files" in rendered
    assert "Invoice_20053.pdf" in rendered
    assert "NEEDS ATTENTION" in rendered


def test_render_clean_when_nothing_unbooked(tmp_path):
    report = SweepReport(scanned=3)
    rendered = render_markdown(report, tenant="demo")
    assert "CLEAN" in rendered
