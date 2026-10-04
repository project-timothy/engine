"""Finding: one condition the owner may need to run down.

Identity is the fingerprint — lens + subject + condition — so a finding seen
again tomorrow is the SAME checklist item, carried forward silently. The
detail line and severity may change day to day without minting a new item
(ages recompute; a condition can worsen).

A finding may also carry a :class:`Candidate`: the automation that would
retire it, as data rather than only as prose in the detail line. Lens 19
(recurrence) is the only producer today, and the triage lane is the consumer
that turns a NAMED candidate into a proposal PR (phase 7 row 7.18). The
candidate is NOT part of the fingerprint: it names an automation, not a
checklist item, so it never mints or moves a finding.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

SEVERITIES = ("INFO", "WARN", "CRITICAL")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Candidate:
    """The automation a repeat points at.

    ``id`` is a function of the SOURCE lens and condition alone, so it is the
    same tonight, tomorrow, and across the three recurrence kinds: one
    automation retires the whole repeat, and a lane keyed on this id proposes
    it exactly once. ``named`` is false when no candidate table names it; the
    triage names it in the note first, and no proposal comes of it.
    """

    id: str
    text: str
    named: bool
    source_lens: str
    source_condition: str
    subjects: tuple[str, ...] = ()
    """The source subjects the repeat was counted over."""
    count: int = 0
    """What the finding counted: nights open, subjects in the class, returns."""


def candidate_id(lens: str, condition: str) -> str:
    """The stable id for the automation that would retire ``lens/condition``."""
    return hashlib.sha256(f"{lens}/{condition}".encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Finding:
    lens: str  # which lens computed it, e.g. "filing"
    subject: str  # the thing it is about, e.g. a file name or "vendor / invoice#"
    condition: str  # the failure class, e.g. "no-disposition"
    severity: str  # INFO | WARN | CRITICAL
    detail: str  # one human line for the report
    candidate: Candidate | None = None  # the automation that would retire it

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}; known: {SEVERITIES}")

    @property
    def fingerprint(self) -> str:
        raw = f"{self.lens}|{self.subject}|{self.condition}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK[severity]
