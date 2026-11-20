"""The small reads of the books the tool modules share: the event log, the
books' log, whether the asker looks over every month, how the books name a
person, and the approved advances. Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..agents.expenses import advances
from .receipts import filer_name
from .viewer import sees_every_month

if TYPE_CHECKING:
    from .catalog import Tools


def _events(t: Tools, *types: str) -> list[tuple[int, dict]]:
    marks = ", ".join("?" for _ in types)
    rows = t._rows(
        f"SELECT id, event_type, payload_json FROM events WHERE tenant = ? "  # noqa: S608  (a fixed column list; values are ? marks)
        f"AND event_type IN ({marks}) ORDER BY id",
        (t.tenant, *types),
    )
    return [
        (r["id"], {"event_type": r["event_type"], **json.loads(r["payload_json"] or "{}")})
        for r in rows
    ]


def _log(t: Tools) -> list[dict]:
    return [
        {
            "event_type": r["event_type"],
            "created_at": r["created_at"],
            "payload": json.loads(r["payload_json"] or "{}"),
        }
        for r in t._rows(
            "SELECT event_type, payload_json, created_at FROM events WHERE tenant = ? ORDER BY id",
            (t.tenant,),
        )
    ]


def _sees_every_month(t: Tools) -> bool:
    """Whether the person looks over every month in the ministry: who may
    set its budget (t-year-budget)."""
    if t.viewer is None:
        return True
    return sees_every_month(t.viewer, t.sees)


def _advances(t: Tools) -> list[dict]:
    """The approved expenses people paid for the ministry themselves (c28)."""
    return advances.approved(t._rows(advances.APPROVED, (t.tenant, advances.ADVANCE_CARD)))


def _name_of(t: Tools, person: str) -> str:
    """How the books carry a person: the name they file under, else their
    name on the ministry's people list, else their id in words."""
    from .viewer import display_name

    root = t.receipts.tenants_root if t.receipts is not None else None
    who = t.viewer.policy.people.get(person) if t.viewer is not None else None
    return (
        filer_name(t.tenant, root, person)
        or (who.full_name if who is not None else "")
        or display_name(person)
    )
