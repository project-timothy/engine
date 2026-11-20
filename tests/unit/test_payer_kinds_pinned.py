"""Every payer of one kind, pinned before a payer could be some of each
(board item t-payer-split; the owner, 2026-11-16).

A payer who sends part pay and part gift changes what the tax packet counts
as pay. A payer who is all pay or all gift must not move by one character:
each packet below was built on main before the split existed, and is held
here byte for byte. The cases walk every kind a payer had: a support
platform, a sending church, an agency's W-2, a personal gift unasked, a no,
a yes, a one-line gift, a deposit with no payer, and a ministry deposit that
carries a payer's name. Only a payer the person split may differ."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_gift_or_pay import _monthly, _w2
from tests.unit.test_payers import MOBILE, PAY, _line, _paid

PINNED = Path(__file__).resolve().parents[1] / "fixtures" / "payer_kinds_pinned.json"


def _platform_aunt_and_a_deposit_with_no_payer(solo):  # noqa: F811
    return _paid(solo)


def _a_gift_said_on_one_line(solo):  # noqa: F811
    door = _paid(solo)
    line = _line(door, MOBILE)["source"]
    door.call(
        "explain_bank_line",
        {"line": line, "said": "my mom's birthday gift to me", "whose": "own", "gift_from": "Mom"},
    )
    door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "form_says": "900.00"})
    return door


def _a_gift_unasked_and_an_organization_asked(solo):  # noqa: F811
    door = _monthly(solo)
    door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER"})
    door.call("keep_payers", {"payer": "Hope Fellowship", "kind": "personal gift"})
    return door


def _a_no_and_a_yes(solo):  # noqa: F811
    door = _a_gift_unasked_and_an_organization_asked(solo)
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "no"})
    door.call("keep_payers", {"payer": "Hope Fellowship", "ministry_work": "yes"})
    return door


def _a_church_and_a_gift_from_the_same_sender(solo):  # noqa: F811
    door = _monthly(solo)
    door.call("keep_payers", {"payer": "Hope Fellowship", "kind": "sending church"})
    door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER"})
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "yes"})
    return door


def _an_agencys_w2(solo):  # noqa: F811
    door = _w2(solo)
    door.call(
        "keep_payers",
        {"payer": "EquipNet", "year": "2026", "form_says": "800.00", "withheld": "75.00"},
    )
    return door


def _a_ministry_deposit_carrying_a_payers_name(solo):  # noqa: F811
    door = _paid(solo)
    line = _line(door, PAY)["source"]
    door.call("explain_bank_line", {"line": line, "said": "ministry money", "whose": "ministry"})
    return door


CASES = {
    "platform, aunt, and a deposit with no payer": _platform_aunt_and_a_deposit_with_no_payer,
    "a gift said on one line": _a_gift_said_on_one_line,
    "a gift unasked and an organization asked": _a_gift_unasked_and_an_organization_asked,
    "a no and a yes": _a_no_and_a_yes,
    "a church and a gift for their work": _a_church_and_a_gift_from_the_same_sender,
    "an agency's W-2": _an_agencys_w2,
    "a ministry deposit carrying a payer's name": _a_ministry_deposit_carrying_a_payers_name,
}


def _packet(solo, name: str) -> dict:  # noqa: F811
    got = CASES[name](solo).call("tax_packet", {"year": "2026"})
    got.pop("next", None)  # the next thing waiting is the door's, never the packet's
    return got


@pytest.mark.parametrize("name", list(CASES))
def test_every_payer_of_one_kind_packs_as_it_did_before_the_split(solo, name):  # noqa: F811
    assert _packet(solo, name) == json.loads(PINNED.read_text(encoding="utf-8"))[name]
