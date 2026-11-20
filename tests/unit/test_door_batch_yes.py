"""One yes for several cards (chunk 7, 2026-10-15). Carol, Grace
Fellowship's treasurer, had five rule cards waiting and got five links and
five Face ID taps, and couldn't tell which were done. Now she says "approve
them all" and her AI asks for every card at once: one link, one page that
lists each card in plain words, one Face ID, and the page says which are
done.

The same rules hold card by card: each is checked before her phone is asked
and again as it is written. A card she can't decide is left out and named,
never dropped silently. A human-only card always gets its own tap, because
the auditor checks those signatures card by card. A card added to the books
after the link went out was never on the list she signed, so it can't ride
along.
"""

# ruff: noqa: F811  (the door and church fixtures, imported)
from __future__ import annotations

import json

import pytest

from core.engine.authority_gate import witnessed_cards
from core.ledger import Ledger
from core.tools.decide import MOST_AT_ONCE, batch_words
from tests.unit.test_door_decide import (
    FakeDesk,
    _all,
    _cards,
    _carol,
    _finish,
    _new_vendor_card,
    _open_door,
    _tools,
    _vouch,
    door,  # noqa: F401  (fixture)
)
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)


def _both(door) -> list[int]:  # noqa: F811
    cards = _cards(door["church"])
    return [cards["Ruth Hollis"]["id"], cards["Tom and Jen Baker"]["id"]]


def _many(tools, cards: list[int], decision: str = "approve", said: str = "") -> dict:
    return tools.call(
        "decide_card", {"cards": cards, "decision": decision, "said": said or "approve them all"}
    )


def _status(church, card: int) -> str:  # noqa: F811
    return next(r for r in _all(church) if r["id"] == card)["status"]


def test_two_cards_get_one_link_and_one_page_that_lists_both(door):
    cards = _both(door)
    said = _many(_carol(door), cards)
    assert said["status"] == "tap_to_confirm" and said["cards"] == cards
    assert said["show"] == "[Tap to approve all 2 with Face ID](https://tim.example.org/yes#link-1)"
    (asked,) = door["desk"].asked
    assert asked["card"] == f"cards:{cards[0]}.{cards[1]}"
    assert asked["summary"] == (
        "Approve these 2:\n"
        "- An expense report for Ruth Hollis, $10.00.\n"
        "- An expense report for Tom and Jen Baker, $10.00."
    )


def test_one_tap_decides_them_all_and_the_page_names_each(door):
    cards = _both(door)
    _many(_carol(door), cards)
    yes = door["desk"].tap()
    assert _finish(door["church"], door["desk"]) == set()
    assert [_status(door["church"], c) for c in cards] == ["approved", "approved"]
    assert door["desk"].settled == [
        (
            yes,
            "approved",
            "Approved all 2: an expense report for Ruth Hollis, $10.00; an expense report "
            "for Tom and Jen Baker, $10.00.",
        )
    ]


@pytest.mark.security("SEC-11")
def test_each_card_keeps_its_own_witness_record_pointing_at_the_one_tap(door):
    cards = _both(door)
    _many(_carol(door), cards)
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    for card in cards:
        params = next(r for r in _all(door["church"]) if r["id"] == card)["params"]
        assert params["decided_by"] == "carol-jennings" and params["decided_via"] == "door"
        evidence = json.loads(params["witness"])
        assert evidence["signature"] == "s"
        assert evidence["batch"] == f"cards:{cards[0]}.{cards[1]}"


@pytest.mark.security("SEC-11")
def test_a_card_she_cannot_decide_is_left_out_and_named(door):
    # Ruth can't approve her own report, and the Bakers' isn't hers to see.
    cards = _both(door)
    ruth = _tools(door["church"], "ruth-hollis", door["desk"], door["asks"])
    said = ruth.call("decide_card", {"cards": cards, "decision": "approve", "said": "approve both"})
    assert said["status"] == "nothing_to_tap" and door["desk"].asked == []
    assert said["left_out"] == [
        "An expense report for Ruth Hollis, $10.00: You can't approve this expense report "
        "through Tim. Someone else on your ministry's list decides it.",
        "One of them isn't waiting for you.",
    ]
    assert "left_out" in said["say"]


def test_with_one_card_left_it_is_the_ordinary_link(door):
    ruth = _both(door)[0]
    said = _many(_carol(door), [ruth, 999])
    assert said["status"] == "tap_to_confirm" and said["card"] == ruth
    assert door["desk"].asked[-1]["card"] == str(ruth)
    assert said["left_out"] == ["One of them isn't waiting for you."]


@pytest.mark.security("SEC-08")
def test_a_human_only_card_needs_its_own_tap(church):  # noqa: F811
    _open_door(church, "expense.report", "vendor")
    vendor = _new_vendor_card(church)
    desk = FakeDesk()
    cards = [*(r["id"] for r in _all(church) if r["action_type"] != "ap.new_vendor_decision")]
    said = _many(_tools(church, "carol-jennings", desk), [*cards, vendor])
    assert desk.asked[-1]["card"] == "cards:" + ".".join(str(c) for c in cards)
    assert said["left_out"] == ["A new vendor named Marion Roofing needs its own tap."]


@pytest.mark.security("SEC-11")
def test_a_card_added_after_the_link_went_out_never_rides_along(door):
    cards = _both(door)
    _many(_carol(door), cards)
    with Ledger.open(door["church"]["ledger_root"]) as ledger:
        run_id = ledger.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        ledger.enqueue_approval(
            idempotency_key="card-late",
            run_id=run_id,
            tenant="grace",
            agent="expenses",
            action_type="expenses.report_review",
            params={"person": "Don Pruitt", "total_cents": "1000"},
        )
    late = _cards(door["church"])["Don Pruitt"]["id"]
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    assert _status(door["church"], late) == "pending"
    assert [_status(door["church"], c) for c in cards] == ["approved", "approved"]


def test_a_card_someone_else_decided_meanwhile_is_named_on_the_page(door):
    cards = _both(door)
    _many(_carol(door), cards)
    door["desk"].tap()
    with Ledger.open(door["church"]["ledger_root"]) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET status = 'approved' WHERE id = ?", (cards[0],)
        )
        ledger.conn.commit()
    _finish(door["church"], door["desk"])
    ((_yes, status, message),) = door["desk"].settled
    assert status == "approved"
    assert message == (
        "Approved 1 of 2: an expense report for Tom and Jen Baker, $10.00. Not decided: an "
        "expense report for Ruth Hollis, $10.00 (someone already decided this expense report)."
    )


@pytest.mark.security("SEC-09")
def test_a_tap_from_someone_elses_phone_decides_none_of_them(door):
    cards = _both(door)
    _many(_carol(door), cards)
    door["desk"].tap(person="ruth-hollis")
    _finish(door["church"], door["desk"])
    ((_yes, status, message),) = door["desk"].settled
    assert status == "refused" and message.startswith("None of these was decided.")
    assert [_status(door["church"], c) for c in cards] == ["pending", "pending"]


def test_asking_again_before_the_tap_is_the_same_link(door):
    cards = _both(door)
    _many(_carol(door), cards)
    said = _many(_carol(door), cards)
    assert said["status"] == "waiting" and said["link"].endswith("#link-1")
    assert len(door["desk"].asked) == 1


def test_asking_after_the_tap_says_where_each_one_stands(door):
    cards = _both(door)
    _many(_carol(door), cards)
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    said = _many(_carol(door), cards)
    assert said["status"] == "nothing_to_tap"
    assert said["done"] == [
        "Approved: an expense report for Ruth Hollis, $10.00.",
        "Approved: an expense report for Tom and Jen Baker, $10.00.",
    ]
    assert len(door["desk"].asked) == 1


def test_a_written_list_run_again_after_a_restart_reads_as_done(door):
    cards = _both(door)
    _many(_carol(door), cards)
    yes = door["desk"].tap()
    tapped = door["desk"].tapped[yes][1]
    from tests.unit.test_door_decide import _finisher

    assert _finisher(door["church"], door["desk"]).finish(tapped)[0] == "approved"
    _finish(door["church"], door["desk"])
    assert door["desk"].settled[-1][1:] == (
        "approved",
        "Approved all 2: an expense report for Ruth Hollis, $10.00; an expense report for "
        "Tom and Jen Baker, $10.00.",
    )


def test_a_no_for_several_declines_them_all(door):
    cards = _both(door)
    _many(_carol(door), cards, "reject", "decline both")
    assert door["desk"].asked[-1]["summary"].startswith("Reject these 2:")
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    assert [_status(door["church"], c) for c in cards] == ["rejected", "rejected"]


def test_with_no_passkey_the_list_goes_to_the_person_named_to_vouch(door):
    _vouch(door["church"], 'carol-jennings = "don-pruitt"')
    door["desk"].no_passkey = {"carol-jennings"}
    cards = _both(door)
    said = _many(_carol(door), cards)
    asked = door["desk"].asked[-1]
    assert asked["person"] == "don-pruitt" and asked["for"] == "carol-jennings"
    assert asked["summary"].startswith("Carol Jennings asked you to confirm their answer:")
    assert said["vouched_by"] == "Don Pruitt"
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    assert [_status(door["church"], c) for c in cards] == ["approved", "approved"]


def test_more_than_ten_waits_for_the_next_link(door):
    cards = _both(door)
    said = _many(_carol(door), [*cards, *range(900, 900 + MOST_AT_ONCE)])
    assert said["left_out"][-1] == (
        "Ten at a time: 2 more wait for the next link once these are done."
    )


@pytest.mark.parametrize("cards", [[], "5", [True], ["x"]])
def test_a_garbled_list_is_refused(door, cards):
    with pytest.raises(ValueError):
        _carol(door).call("decide_card", {"cards": cards, "decision": "approve", "said": "x"})


def test_the_list_fits_the_approve_page_however_long_each_card_is(door):
    row = _all(door["church"])[0]
    long = {**row, "params": {**row["params"], "what": "the rule " + "word " * 300}}
    summary = batch_words([long] * MOST_AT_ONCE, "approve")
    assert len(summary) <= 2000 and summary.count("\n- ") == MOST_AT_ONCE


def test_a_witness_names_its_one_card_or_each_card_on_its_list():
    assert witnessed_cards("12") == ("12",)
    assert witnessed_cards("cards:5.6.17") == ("5", "6", "17")
    assert witnessed_cards("cards:5.x.6") == ("5", "6")


def _audit_ctx(church):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from auditor.ledger_reader import LedgerReader
    from auditor.lenses import AuditContext

    return AuditContext(
        tenant=SimpleNamespace(slug="grace", stale_approval_days=30),
        ledger=LedgerReader.open(church["ledger_root"]),
        now=datetime.now(UTC),
        store_root=church["root"],
        tenants_dir=church["root"],
    )


def test_the_auditor_reads_a_list_tap_the_same_as_single_taps(door):
    # The auditor's approvals lens, untouched by chunk 7, over books where one
    # tap decided two cards: nothing to report.
    from auditor.lenses import approvals

    cards = _both(door)
    _many(_carol(door), cards)
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    ctx = _audit_ctx(door["church"])
    with ctx.ledger:
        assert approvals.check_human_only(ctx) == []
        assert approvals.check_word_line(ctx) == []


def test_why_a_human_only_card_never_rides_a_list(church):
    # The auditor checks a human-only card's signature card by card. One Face
    # ID signs the list, never each card's number, so the auditor would
    # (rightly) not stand behind a human-only card decided on a list.
    import base64

    from auditor.lenses import approvals

    def b64u(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    _open_door(church, "vendor")
    card = _new_vendor_card(church)
    nonce = b"n" * 32
    signed = approvals.decision_challenge(
        nonce, "carol-jennings", f"cards:{card}.1", "approve", "s"
    )
    client = {"type": "webauthn.get", "challenge": b64u(signed)}
    evidence = {"nonce": b64u(nonce), "client_data": b64u(json.dumps(client).encode())}
    params = {
        "decided_by": "carol-jennings",
        "decided_via": "door",
        "witness_resource": "vendor",
        "witness": json.dumps({**evidence, "signature": "s", "summary": "s"}),
    }
    ctx = _audit_ctx(church)
    with ctx.ledger:
        refused = approvals.door_refusal(ctx, {"id": card, "status": "approved"}, params)
    assert refused == "the signed evidence is not carol-jennings's approve on this card"
