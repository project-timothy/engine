"""What the ``talk`` eval set shows a model (issue #681).

The first live trial of Tim's voice (2026-11-18) validated every turn and
still went wrong: he said the books weren't connected and called true
figures made up. The set (``core/llm/eval_sets/talk``) puts a model in five
moments of a conversation and checks the turn it takes, so a ministry can't
name a model for ``talk`` or ``talk_hard`` until it has shown it reaches
for the books (``core.llm.evals``, the gate at ``load_tenant``).

:func:`system` is the system text every case is sent: Tim's own brief and
manual (``core.talk.loop._system``) over the tools a made-up solo
missionary has on a box, on a fixed day so each case's dates hold. Built
when the set runs, so a set scored today measures today's brief.
"""

from __future__ import annotations

from types import SimpleNamespace

from ..tools import catalog
from .loop import _system

PERSON = "daniel-reyes"
MINISTRY = "Riverstone Outreach, Inc."
TODAY = "2026-11-18"

TOOLS = (
    *catalog.SPECS,
    catalog.DECIDE,
    catalog.ADD_RECEIPT,
    catalog.PAID_IT_MYSELF,
    catalog.TAX_PACKET,
    catalog.KEEP_PAYERS,
    catalog.SET_CATEGORY,
    catalog.SET_AMOUNT,
    catalog.SET_DRIVE_PURPOSE,
    catalog.CATCH_UP_LINK,
    catalog.SET_RECEIPT_DAY,
    catalog.RECEIPTS_TO_READ,
    catalog.RECORD_READING,
    catalog.AGREE_TO_READINGS,
    catalog.SEND_MONTH_FOR_REVIEW,
    *catalog.STATEMENT_TOOLS,
)
"""A solo missionary's tools on her box's door, in the order the catalog
offers them (``core.tools.catalog.Tools``)."""


def system() -> str:
    """Tim's system text for the made-up missionary, as the loop builds it."""
    who = SimpleNamespace(viewer=SimpleNamespace(person=PERSON), ministry=MINISTRY, today=TODAY)
    return _system(who, {spec.name: spec for spec in TOOLS})


__all__ = ["MINISTRY", "PERSON", "TODAY", "TOOLS", "system"]
