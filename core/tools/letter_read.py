"""Tim reads her IRS letter on the interview's letter screen (Standalone Tim,
card R2).

Until now she typed her ministry's legal name off the letter, exactly as
it prints it, on her phone. Where the box's own file switches it on, the
screen reads the photo she just took and says back the legal name and the
Employer Identification Number in one plain line. Her tap fills what she
would have typed; "No" leaves her the box she has today. The photo is kept
as today. Nothing a model read is written anywhere until her tap: the
reading rides in the confirming form, and her yes posts it as her answer.

- **Off unless the file names it.** Job type ``letter_read`` in
  ``[llm.jobs]`` (``core.llm.policy.NAMED_ONLY``), held by its own eval set
  (``core/llm/eval_sets/letter_read``, made-up letters). The interview has no
  ministry yet, so the file is the box's own: whoever runs the box pays for
  the read, as it pays for the box.
- **Code decides what is shown.** A letter that isn't the IRS's, or isn't
  to a nonprofit (an LLC's number, a trust's), a name that is the IRS's own,
  or a number that can't be an Employer Identification Number: no reading,
  and she types as today.

Boundary classification (docs/boundary-rules.md): the model reads a photo
into two fields; code checks both, and her tap is the only writer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..onboarding.interview import ein_of
from . import doc_read

JOB = "letter_read"
"""The job type a box's ``[llm.jobs]`` names to switch the reader on."""

SYSTEM = """\
Reply with ONLY a JSON object (no prose, no code fences) with keys:
  doc_type: "irs_letter" if this is a letter from the Internal Revenue Service
    to an organization (a determination letter saying it is exempt from
    federal income tax, or the notice assigning it an Employer Identification
    Number), else "other"
  organization: "nonprofit" if the letter is to a nonprofit, a church, or an
    organization exempt from federal income tax; "other" if it is to a
    business, a partnership, a limited liability company, a trust, an estate,
    or a person; null if the letter does not say
  legal_name: the organization's name exactly as the letter prints it (the
    name in the address block, or the line that names it), with its
    capitals and punctuation, or null
  ein: the Employer Identification Number as printed (nine digits, like
    12-3456789), or null
Read every page. The Internal Revenue Service's own name and address are
never the organization's. Do not guess a name or a number that is not
printed.
"""
"""The reader's brief; the eval set carries the same words (a test pins it)."""

USER = (
    "Read the attached letter. It is a photo or a scan with no text layer, so read the "
    "picture itself."
)

_THE_IRS = re.compile(r"internal\s+revenue|department\s+of\s+the\s+treasury|\birs\b", re.I)


class LetterReply(BaseModel):
    """What the model says the letter prints."""

    doc_type: str = Field(default="other", max_length=40)
    organization: str | None = Field(default=None, max_length=40)
    legal_name: str | None = Field(default=None, max_length=300)
    ein: str | None = Field(default=None, max_length=40)


@dataclass(frozen=True)
class LetterReading:
    """A letter's reading that code checked: the legal name and the
    Employer Identification Number, as "12-3456789"."""

    legal_name: str
    ein: str


def on(settings: Any) -> bool:
    """Whether the box's file switches the reader on."""
    return doc_read.on(settings, JOB)


def read(
    photo: bytes,
    suffix: str,
    *,
    settings: Any,
    ledger_root: str | Path,
    tenant: str,
    today: date,
    adapter: Any = None,
) -> LetterReading | None:
    """What her letter reads, or None whenever it can't be trusted to show
    her (:func:`checked`), or there is no reading at all."""
    reply = doc_read.ask(
        photo,
        suffix,
        job=JOB,
        system=SYSTEM,
        user=USER,
        output_model=LetterReply,
        settings=settings,
        ledger_root=ledger_root,
        tenant=tenant,
        person="interview",
        today=today,
        adapter=adapter,
    )
    return checked(reply) if reply is not None else None


def checked(reply: LetterReply) -> LetterReading | None:
    """The reply as a :class:`LetterReading`, or None. Code decides: the
    IRS's letter to a nonprofit, a name with letters in it that isn't the
    IRS's own, and a number that can be an Employer Identification Number."""
    if reply.doc_type != "irs_letter" or reply.organization != "nonprofit":
        return None
    name = " ".join(
        "".join(c if c.isprintable() else " " for c in str(reply.legal_name or "")).split()
    )
    if not 2 <= len(name) <= 120 or not re.search(r"[^\W\d_]", name) or _THE_IRS.search(name):
        return None
    ein = ein_of(str(reply.ein or ""))
    if not ein:
        return None
    return LetterReading(name, ein)


__all__ = ["JOB", "SYSTEM", "USER", "LetterReading", "LetterReply", "checked", "on", "read"]
