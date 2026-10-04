"""Security review 2026-10-03 (finding 1 / #389, MEDIUM): a parse has a deadline.

Four published pypdf advisories are reachable through ``PdfReader`` and
``extract_text()``: a crafted PDF makes the parse run for a very long time.
Nothing bounded it, and the job runs inside the ledger's write lock, so one
emailed PDF stalled every later stage of the morning run until the process
was killed. Every pypdf read site now runs under a wall-clock deadline and a
blown deadline takes the site's existing failure path.
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import pytest

from core.engine import timebox
from core.engine.timebox import ParseTimeout, time_limit


class _Hangs:
    """Stands in for ``pypdf.PdfReader`` on a crafted file: never returns."""

    def __init__(self, *_a, **_k) -> None:
        while True:
            pass


@pytest.fixture
def hanging_pypdf(monkeypatch):
    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", _Hangs)
    monkeypatch.setattr(timebox, "PDF_PARSE_SECONDS", 0.2)


def test_the_limit_interrupts_a_spinning_parse_and_restores_the_signal():
    before = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(ParseTimeout), time_limit(0.2, "x.pdf"):
        while True:
            pass
    assert time.monotonic() - started < 2
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_fast_parse_is_untouched():
    with time_limit(5, "x.pdf"):
        value = sum(range(1000))
    assert value == 499500
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_off_the_main_thread_the_limit_stands_aside():
    seen: list[object] = []

    def work():
        with time_limit(0.1, "x.pdf"):
            seen.append("ran")

    t = threading.Thread(target=work)
    t.start()
    t.join(5)
    assert seen == ["ran"]


@pytest.mark.usefixtures("hanging_pypdf")
def test_the_text_layer_read_gives_up_and_reads_as_no_text(tmp_path):
    from core.llm.rasterize import pdf_text

    doc = tmp_path / "crafted.pdf"
    doc.write_bytes(b"%PDF-1.4 crafted")
    started = time.monotonic()
    assert pdf_text(doc) == ""
    assert time.monotonic() - started < 2


@pytest.mark.usefixtures("hanging_pypdf")
def test_a_hanging_statement_is_a_statement_error(tmp_path):
    from core.adapters.bank_statement_pdf import BankStatementError, pdf_text

    doc = tmp_path / "stmt 2026-09-30.pdf"
    doc.write_bytes(b"%PDF-1.4 crafted")
    with pytest.raises(BankStatementError, match="ran past"):
        pdf_text(doc)


@pytest.mark.usefixtures("hanging_pypdf")
def test_a_hanging_scan_counts_one_page_and_never_splits(tmp_path):
    from core.agents.expenses.scan_split import pdf_page_count, split_pdf

    doc = tmp_path / "scan.pdf"
    doc.write_bytes(b"%PDF-1.4 crafted")
    assert pdf_page_count(doc) == 1
    with pytest.raises(ParseTimeout):
        split_pdf(doc, [])


@pytest.mark.usefixtures("hanging_pypdf")
def test_a_hanging_w9_candidate_returns_inside_the_deadline(tmp_path):
    from core.agents.ap.w9 import UNREADABLE, detect

    doc = tmp_path / "scan0001.pdf"
    doc.write_bytes(b"%PDF-1.4 crafted")
    started = time.monotonic()
    assert detect(Path(doc)) in (UNREADABLE, None)
    assert time.monotonic() - started < 2
