"""Setting a missionary's pay through the box's door (c34, the sender model,
the owner's go of 2026-11-14).

A board member tells their own AI what the board decided ("Ruth's pay for
2027 is $24,000, paid on the 1st"). Tim drafts the resolution and raises
its one card; nothing is paid, and nothing is in force until a director
with no family tie says yes. Only a church's door offers it, and only to
someone who decides the ministry's rules.
"""

from __future__ import annotations

from pathlib import Path

from core.agents.policy.evals.test_pay_card import _pay_card, church  # noqa: F401  (fixture)
from core.engine.runner import resolve_ledger_root
from core.tools import catalog
from core.tools.receipts import ReceiptDesk, Receipts
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door as _solo_door


def _door(church, person: str) -> catalog.Tools:  # noqa: F811
    ledger_root = Path(resolve_ledger_root("grace"))
    return catalog.Tools(
        "grace",
        ledger_root=ledger_root,
        viewer=viewer_for("grace", person, tenants_root=church["root"]),
        receipts=Receipts(
            "grace",
            tenants_root=church["root"],
            ledger_root=ledger_root,
            person=person,
            desk=ReceiptDesk(),
            base_url="https://tim.example.org/receipt",
        ),
        today="2026-11-14",
    )


def _names(church, person: str) -> set[str]:  # noqa: F811
    return {s.name for s in _door(church, person).specs()}


def test_a_church_offers_set_pay_to_who_decides_its_rules(church):  # noqa: F811
    assert "set_pay" in _names(church, "don-pruitt")
    assert "set_pay" not in _names(church, "ruth-hollis")  # a missionary
    assert "set_pay" not in _names(church, "carol-jennings")  # the treasurer pays; the board sets


def test_a_solo_ministry_is_not_offered_set_pay(solo):  # noqa: F811
    names = {s.name for s in _solo_door(solo, "daniel-reyes").specs()}
    assert "set_pay" not in names


def test_set_pay_drafts_the_resolution_and_waits_for_a_yes(church):  # noqa: F811
    said = _door(church, "don-pruitt").call(
        "set_pay", {"person": "Ruth Hollis", "yearly": "$24,000", "day": 1, "from": "2027-01"}
    )
    assert said["status"] == "drafted", said
    words = said["say"]
    assert "Ruth Hollis" in words and "$24,000 a year" in words
    assert "January 2027 through December 2027" in words
    assert "no family tie" in words and "Tim never sends" in words
    assert _pay_card()["status"] == "pending"


def test_a_pay_that_cannot_be_set_comes_back_in_plain_words(church):  # noqa: F811
    said = _door(church, "don-pruitt").call(
        "set_pay", {"person": "Ruth Hollis", "yearly": "24000", "day": 31, "from": "2027-01"}
    )
    assert said["status"] == "refused"
    assert said["say"] == "The pay day is a day of the month, 1 to 28."
