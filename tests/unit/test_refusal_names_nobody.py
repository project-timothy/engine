"""A refusal names nobody the person may not act for (the weekly attacker,
2026-10-16; the owner called it sev:low on 2026-10-22).

Ruth asked for a catch-up link for "Tom Baker" and heard "Tom and Jen
Baker's receipts aren't in a month they review": no record leaked, but the
door said back a full name she never gave, and a refusal that differs from
"nobody by that name" tells her who exists. The door now looks only among the
people she may act for, so someone outside them reads exactly like a name
nobody has.
"""

from __future__ import annotations

from core.tools import catalog
from core.tools.receipts import ReceiptDesk, Receipts
from core.tools.viewer import viewer_for
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)


def _door(church, person: str) -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "grace",
        ledger_root=church["ledger_root"],
        viewer=viewer_for("grace", person, tenants_root=church["root"]),
        receipts=Receipts(
            "grace",
            tenants_root=church["root"],
            ledger_root=church["ledger_root"],
            person=person,
            desk=ReceiptDesk(),
            base_url="https://tim.example.org/receipt",
        ),
        today="2026-10-13",
    )


def _ask(church, person: str, whose: str) -> dict:  # noqa: F811
    return _door(church, person).call("catch_up_link", {"person": whose})


def test_someone_outside_their_care_reads_like_a_name_nobody_has(church):  # noqa: F811
    baker = _ask(church, "ruth-hollis", "Tom Baker")
    nobody = _ask(church, "ruth-hollis", "Tom Nobody")
    assert baker["status"] == nobody["status"] == "not_found"
    assert "Jen" not in baker["say"] and "Baker's" not in baker["say"]
    assert baker["say"] == nobody["say"].replace("Tom Nobody", "Tom Baker")


def test_the_person_who_reviews_them_still_finds_them(church):  # noqa: F811
    assert _ask(church, "carol-jennings", "Tom Baker")["status"] == "tap_to_add"
    assert _ask(church, "ruth-hollis", "Ruth")["status"] == "tap_to_add"
