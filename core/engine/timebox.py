"""A wall-clock deadline for parsing untrusted documents.

Security review 2026-10-03 (#389): published pypdf advisories let a crafted
PDF make ``PdfReader`` or ``extract_text()`` run for a very long time. Jobs run
inside the ledger write lock, so one emailed file used to stall every later
stage of the morning run. Every pypdf read site runs under :func:`time_limit`
and turns :class:`ParseTimeout` into its existing failure path.

pypdf is pure Python, so ``SIGALRM`` interrupts it between bytecodes. A signal
can only be installed from the main thread; elsewhere the limit stands aside
rather than failing the caller (every scheduled job runs on the main thread).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

PDF_PARSE_SECONDS: float = 60.0
"""Generous for a real statement or invoice (they parse in well under a
second); a parse still running at a minute is a crafted file, not a slow one."""


class ParseTimeout(TimeoutError):
    """A document parse ran past its deadline."""


@contextmanager
def time_limit(seconds: float, what: str) -> Iterator[None]:
    """Raise :class:`ParseTimeout` in the body after ``seconds`` of wall time.
    Restores whatever handler and timer were in place before."""
    usable = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if not usable or seconds <= 0:
        yield
        return

    def _expired(_signum, _frame):
        raise ParseTimeout(f"parsing {what} ran past {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    prior_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if prior_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *prior_timer)


def pdf_deadline(what: str):
    """The deadline every pypdf read site uses (looked up at call time so a
    test can shorten it)."""
    return time_limit(PDF_PARSE_SECONDS, what)
