"""One document read by a model for a page that is not a job (Standalone
Tim, card R2): the IRS letter on the interview's letter screen, a bank
statement sent through a catch-up link.

Both readers ask the same way, so the asking lives here once:

- **Off unless the ministry's file names the job.** Each reader is its own
  job type in ``core.llm.policy.NAMED_ONLY``: a ``default`` tier never
  switches it on, and the eval gate holds the tier it names to its own set
  (``core/llm/eval_sets/<job>``).
- **The ledger is never held across the call.** The page runs outside any
  job, so the call is priced, recorded, and capped through
  ``complete_metered`` and the talk :class:`~core.talk.meter.Meter`, the way
  the receipt page's reader (card R1) and Tim's own voice are.
- **Any failure is "no reading".** No line, a bare ``fixture`` tier, a tier
  ``[llm].document_tiers`` refuses, a reply that doesn't validate, a timeout,
  the month's cap: :func:`ask` returns None and the page answers the way it
  did before the reader existed.

What a reply MEANS is each reader's own business, checked by code there
(``letter_read.checked``, ``statement_read.check``): a model's reading is
never written anywhere until code has checked it and the person said yes.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..llm.policy import named_on

TIMEOUT_S = 60
"""How long a page waits on the model before it answers the way it always
has. A letter or a statement is a page or two of small print."""

READABLE = (".jpg", ".png", ".pdf")
"""What a reader is shown: a photo or a PDF. A HEIC photo is kept and
answered the way it always was."""


def ask[T: BaseModel](
    data: bytes,
    suffix: str,
    *,
    job: str,
    system: str,
    user: str,
    output_model: type[T],
    settings: Any,
    ledger_root: str | Path,
    tenant: str,
    person: str,
    today: date,
    adapter: Any = None,
    timeout_s: int = TIMEOUT_S,
) -> T | None:
    """The model's validated reply for one document, or None whenever there
    is none to check: ``job`` is off, the file is not a photo or a PDF, or
    the call failed, was refused, timed out, or hit the month's cap.
    ``adapter`` is for tests; the ministry's tier is used otherwise."""
    from ..agents.ap.extraction import document_mime
    from ..llm.gateway import Attachment, Message
    from ..llm.policy import complete_metered
    from ..talk.meter import Meter

    if not named_on(settings, job) or suffix not in READABLE:
        return None
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"document{suffix}"
        path.write_bytes(data)
        try:
            result = complete_metered(
                settings,
                Meter(ledger_root),
                job,
                [Message("system", system), Message("user", user)],
                output_model,
                tenant=tenant,
                run_key=f"{job}:{person}:{today.isoformat()}",
                attachments=[Attachment(path, document_mime(path))],
                timeout_s=timeout_s,
                adapter=adapter,
                redact_detail=True,  # a failed reply may quote the letter or the statement
            )
        except Exception:  # noqa: BLE001  any failure: the page answers as it always has
            return None
    return result.output


def on(settings: Any, job: str) -> bool:
    """Whether the ministry's file switches ``job`` on."""
    return named_on(settings, job)


__all__ = ["READABLE", "TIMEOUT_S", "ask", "on"]
