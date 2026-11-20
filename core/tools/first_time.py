"""The first time: what is happening, why, and the rule, once
(docs/screens/README.md, chunk 2; the owner, 2026-11-16).

A capability shows up when it is needed, never before. The first time a
person meets one of these, the answer through their door carries
``first_time``: what is happening, why, and the rule, the rule word for word
from Tim's own FAQ answer (``FAQ``). After that, nothing more: the answer
itself is the one line. ``why`` brings the full words back any time.

1. ``face_id``: their first yes over the dollar line that takes Face ID
   (core/tools/decide.py); the yes page carries the why and the rule too.
2. ``cash``: an ATM withdrawal on a statement line of theirs.
3. ``other_currency``: a receipt of theirs in another currency.
4. ``split``: a receipt of theirs that was partly their own.
5. ``drives``: a drive from their phone.
6. ``month_sent``: the first time a month of theirs is ready to send.

Two to six come from the person's books, whichever answer comes next, one
at a time; the first is the moment of the yes. Tim remembers per person what
it has explained in ``shown.json`` beside ``setup.json``
(core/onboarding/person.PeopleFile, mode 0600, no ledger migration). A door
with no such file (the owner's own local use) explains nothing. Boundary
classification (docs/boundary-rules.md): **code**. No model picks a moment.
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from ..agents.expenses import statements
from ..agents.expenses.records import DRIVE_EVENT, RECEIPT_SPLIT_EVENT
from ..onboarding.person import PeopleFile
from .catalog import ToolSpec, _schema, register
from .viewer import unit_of

WORDS_FILE = Path(__file__).with_name("first_time.toml")
FILE = "shown.json"

MOMENTS = ("face_id", "cash", "other_currency", "split", "drives", "month_sent")
"""Every first-time moment, in the order they're explained."""


@cache
def _file() -> dict:
    return tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))


FAQ: dict[str, str] = {m: str(_file()[m]["faq"]) for m in MOMENTS}
"""The FAQ answer each moment's rule is taken from."""


def words(moment: str) -> dict[str, str]:
    """The full words for ``moment``: what, why, the rule, and the once line."""
    held = _file()[moment]
    return {
        "about": moment,
        "what": str(held["what"]),
        "why": str(held["why"]),
        "rule": str(held["rule"]),
        "once": str(_file()["once"]),
    }


def page_words(moment: str) -> str:
    """The why and the rule as the yes page shows them under the card: the
    doorkeeper sets the lines after a blank line apart, and only when the
    yes is Face ID's (an emailed link takes none)."""
    said = words(moment)
    return f"\n\nWhy: {said['why']}\nThe rule: {said['rule']}"


class Shown(PeopleFile):
    """What Tim has explained to each person, and when."""

    def __init__(self, directory: str | Path) -> None:
        super().__init__(directory, FILE)

    def seen(self, person: str) -> set[str]:
        held = self._read().get(person, {})
        return set(held) if isinstance(held, dict) else set()

    def mark(self, person: str, moment: str, *, now: str | None = None) -> bool:
        """``moment`` explained to ``person``: True the first time."""
        with self._lock:
            held = self._read()
            mine = held.setdefault(person, {})
            if moment in mine:
                return False
            mine[moment] = now or datetime.now(UTC).isoformat(timespec="seconds")
            self._write(held)
            return True


def due(log: list[dict], me: str, *, next_kind: str, seen: set[str]) -> str | None:
    """The first moment ``me`` meets in their books and hasn't been shown."""
    mine = [e for e in log if unit_of(str(e["payload"].get("person", ""))) == me]
    tags, landed = statements._receipt_tags(log)
    receipts = {sha for sha, p in landed.items() if unit_of(str(p.get("person", ""))) == me}
    found = {
        "cash": any(
            e["event_type"] == statements.LINE_EVENT
            and statements.Line.of(e["payload"]).kind == statements.ATM
            for e in mine
        ),
        "other_currency": any(
            sha in receipts and (statements.amount_of(tag) or ("USD",))[0] != "USD"
            for sha, _day, tag, _person in tags
        ),
        "split": any(
            e["event_type"] == RECEIPT_SPLIT_EVENT and e["payload"].get("sha256") in receipts
            for e in log
        ),
        "drives": any(e["event_type"] == DRIVE_EVENT for e in mine),
        "month_sent": next_kind == "month_ready",
    }
    return next((m for m in MOMENTS[1:] if found[m] and m not in seen), None)


def closing(t: Any, log: list[dict], nxt: dict) -> dict | None:
    """The first-time words an answer through ``t``'s door carries, if any,
    marked shown as they go out."""
    if t.viewer is None or getattr(t, "shown", None) is None:
        return None
    me = t.viewer.person
    moment = due(log, me, next_kind=str(nxt.get("kind", "")), seen=t.shown.seen(me))
    if moment is None or not t.shown.mark(me, moment):
        return None
    return words(moment)


def face_id_due(t: Any) -> bool:
    """Whether this person's yes, if it takes Face ID, is their first."""
    shown = getattr(t, "shown", None)
    return (
        t.viewer is not None and shown is not None and "face_id" not in shown.seen(t.viewer.person)
    )


def _why(t: Any, args: dict) -> dict:
    about = str(args.get("about", "")).strip()
    if about not in MOMENTS:
        return {
            "status": "needs",
            "say": "Ask which one: a yes that takes Face ID (face_id), cash from a machine "
            "(cash), a receipt in another currency (other_currency), a receipt partly yours "
            "(split), drives (drives), or sending a month (month_sent).",
        }
    return {**words(about), "say": "Say the what, the why, and the rule, the rule as it is."}


WHY = register(
    ToolSpec(
        "why",
        "The full words again for something Tim explained the first time it came up: what is "
        "happening, why, and the rule. Use when the person asks why, or asks to hear it again. "
        "about: face_id (a yes that takes Face ID), cash (cash from a machine), "
        "other_currency (a receipt in another currency), split (a receipt partly theirs), "
        "drives (drives from their phone), or month_sent (sending a month for review).",
        _schema(
            {"about": {"type": "string", "enum": list(MOMENTS), "description": "What to explain."}},
            required=["about"],
        ),
        _why,
        short=(
            "The full words again for something Tim explained the first time it came up, when the"
            " person asks why or to hear it again. about: face_id (a yes that takes Face ID), "
            "cash, other_currency, split, drives, or month_sent."
        ),
    ),
    lambda t: t.viewer is not None,
)
