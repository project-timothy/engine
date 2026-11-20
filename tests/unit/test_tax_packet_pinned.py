"""The tax packet's answer, pinned (board chunk c36, PR 3): the packet's tax
half moved out of the door's catalog into ``packet.py``, and what Tim says
did not change by one character. The fixture is the answer as it stood
before the move: a support platform's 1099 against what Tim saw arrive, a
personal gift kept off, a deposit with no payer, a deposit to ask about, a
cost paid from their own pay, self-employment tax, and the four estimates."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_payers import _from_pay, _line

PINNED = Path(__file__).resolve().parents[1] / "fixtures" / "tax_packet_pinned.json"


def test_the_tax_packet_answers_as_it_did_before_the_move(solo):  # noqa: F811
    door = _from_pay(solo)
    line = _line(door, "TEAM TAXI KATHMANDU")["source"]
    door.call("explain_bank_line", {"line": line, "said": "from my pay", "whose": "pay"})
    door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "form_says": "1000.00"})
    door.call("keep_tax_facts", {"year": "2026", "support_elsewhere": "none"})  # Y07's ask
    got = door.call("tax_packet", {"year": "2026"})
    # c38 added what the person told Tim; c39 changed the fixture on purpose:
    # the federal return to a bracket, and next year's estimates under the
    # Form 1040-ES rules (none here: under $1,000 owed). The rest stands.
    assert got.pop("what_they_said") == {
        "filing_status": "",
        "estimates_paid": [],
        "support_elsewhere": "none",
        "trips": [],
    }
    got.pop("next")  # every door answer ends with the next thing since chunk 1 (2026-11-16)
    assert got == json.loads(PINNED.read_text(encoding="utf-8"))


def test_the_door_holds_no_tax_arithmetic():
    from core.tools import catalog

    assert not hasattr(catalog, "_packet_taxes")
