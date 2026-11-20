"""A person takes back their own yes or no until the next run acts on it
(the owner's call of 2026-11-18; the persona year's A06, run 3: Eli asked Tim
to undo an approval he had just given, and Tim had to say he couldn't).

The card goes back to waiting with who took it back, when, and the decision
it carried written on it, so the 08:00 run and the auditor see an ordinary
waiting card. Refused once any run of the ministry has started since the
decision (a door write starts one, and a run acts on approved cards), for
someone else's decision, for a human-only card, for a card more than one yes
went into, and without the person's own words asking for it.
"""

from __future__ import annotations

import json

import pytest

from core.ledger import Ledger
from core.llm.adapters.fixture import FixtureAdapter
from core.tools.decide import TAKE_BACK
from tests.unit.test_door_decide import (
    _carol,
    _decide,
    _ruths,
    _word_line,
    door,  # noqa: F401  (fixture)
)
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_talk_loop import call, say, voice_on
from tests.unit.test_talk_yes import ask, books, box, card_id
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)

UNDO = "Oops, take back my yes on Ruth's report"


def _approved(door) -> int:  # noqa: F811
    with Ledger.open(door["church"]["ledger_root"]) as ledger:  # the fixture's run says "now"
        ledger.conn.execute("UPDATE runs SET created_at = '2026-10-01T00:00:00+00:00'")
        ledger.conn.commit()
    _word_line(door["church"], "500")
    card = _ruths(door["church"])["id"]
    assert _decide(_carol(door), card, said="Approve Ruth's report")["status"] == "approved"
    return card


def _take_back(tools, card: int, said: str = UNDO) -> dict:
    return tools.call("decide_card", {"card": card, "decision": TAKE_BACK, "said": said})


def test_her_own_yes_is_taken_back_and_the_card_waits_again(door):  # noqa: F811
    card = _approved(door)
    out = _take_back(_carol(door), card)
    assert out["status"] == "taken_back" and out["card"] == card
    assert "an expense report for Ruth Hollis, $10.00" in out["say"]
    row = _ruths(door["church"])
    assert row["status"] == "pending" and row["resolved_at"] is None
    for key in ("decided_by", "decided_via", "yes_by_word_up_to", "authority_reason"):
        assert key not in row["params"]
    [back] = json.loads(row["params"]["taken_back"])
    assert back["by"] == "carol-jennings" and back["was"] == "approved"
    assert back["said"] == UNDO and back["stamp"]["decided_by"] == "carol-jennings"


def test_a_card_taken_back_can_be_decided_again(door):  # noqa: F811
    card = _approved(door)
    _take_back(_carol(door), card)
    again = _decide(_carol(door), card, "reject", "Decline Ruth's report")
    assert again["status"] == "rejected"
    row = _ruths(door["church"])
    assert row["status"] == "rejected" and row["params"]["decided_by"] == "carol-jennings"
    assert len(json.loads(row["params"]["taken_back"])) == 1  # the record stays


def test_a_no_is_taken_back_too(door):  # noqa: F811
    card = _approved(door)
    _take_back(_carol(door), card)
    _decide(_carol(door), card, "reject", "Decline Ruth's report")
    out = _take_back(_carol(door), card, "undo that, I didn't mean to decline it")
    assert out["status"] == "taken_back" and _ruths(door["church"])["status"] == "pending"
    assert len(json.loads(_ruths(door["church"])["params"]["taken_back"])) == 2


def test_once_a_run_has_started_since_it_stands(door):  # noqa: F811
    card = _approved(door)
    with Ledger.open(door["church"]["ledger_root"]) as ledger:
        ledger.record_run(
            idempotency_key="after-the-yes",
            tenant="grace",
            agent="expenses",
            job="report",
            status="ok",
            shadow=False,
            result_json="{}",
        )
    with pytest.raises(Exception, match="run since"):
        _take_back(_carol(door), card)
    assert _ruths(door["church"])["status"] == "approved"


def test_only_her_own_words_take_it_back(door):  # noqa: F811
    card = _approved(door)
    with pytest.raises(Exception, match="own words"):
        _take_back(_carol(door), card, said="is Ruth's report all set?")
    assert _ruths(door["church"])["status"] == "approved"


def test_someone_elses_decision_is_theirs_to_take_back(door):  # noqa: F811
    from tests.unit.test_door_decide import _tools

    card = _approved(door)
    don = _tools(door["church"], "don-pruitt", door["desk"], door["asks"])
    with pytest.raises(Exception, match="someone else"):
        _take_back(don, card)
    assert _ruths(door["church"])["status"] == "approved"


def test_a_card_still_waiting_has_nothing_to_take_back(door):  # noqa: F811
    card = _ruths(door["church"])["id"]
    with pytest.raises(Exception, match="nothing to take back"):
        _take_back(_carol(door), card)


def test_several_cards_are_taken_back_one_at_a_time(door):  # noqa: F811
    card = _approved(door)
    with pytest.raises(Exception, match="one at a time"):
        _carol(door).call("decide_card", {"cards": [card], "decision": TAKE_BACK, "said": UNDO})


@pytest.mark.parametrize(
    ("said", "asks"),
    [
        (UNDO, True),
        ("undo that approval", True),
        ("Please take it back", True),
        ("I changed my mind, reverse it", True),
        ("can you unapprove the printer?", True),
        ("approve Ruth's report", False),
        ("what did I approve?", False),
    ],
)
def test_what_asks_to_take_it_back(said, asks):
    from core.tools.decide import asks_to_take_back

    assert asks_to_take_back(said) is asks


# ---- on Tim's own page ------------------------------------------------------------------


def _status(where, key: str) -> str:
    return next(c[1] for c in books(where)["cards"] if c[0] == card_id(where, key))


def _approve_august(solo) -> int:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:  # a fixture's run may say "now"
        ledger.conn.execute("UPDATE runs SET created_at = '2026-10-01T00:00:00+00:00'")
        ledger.conn.commit()
    small = card_id(solo, "card-small")
    model = FixtureAdapter(
        [call("decide_card", card=small, decision="approve", said="approve"), say("Done.")]
    )
    ask(solo, "approve my August report", model)
    assert _status(solo, "card-small") == "approved"
    return small


def test_on_tims_page_her_words_take_back_her_own_yes(solo):  # noqa: F811
    voice_on(solo)
    small = _approve_august(solo)
    model = FixtureAdapter(
        [call("decide_card", card=small, decision=TAKE_BACK, said="x"), say("It's taken back.")]
    )
    answer = ask(solo, "oops, can you take back my yes on the August report?", model)
    assert answer.calls == ["decide_card"] and answer.say == "It's taken back."
    assert _status(solo, "card-small") == "pending"
    assert '"status": "taken_back"' in model.calls[1].turns()[-1].content
    back = json.loads(next(c for c in books(solo)["cards"] if c[0] == small)[2])["taken_back"]
    assert "take back my yes" in json.loads(back)[0]["said"]  # her own message, never the model's


def test_on_tims_page_a_take_back_she_never_asked_for_is_refused(solo):  # noqa: F811
    voice_on(solo)
    small = _approve_august(solo)
    model = FixtureAdapter(
        [
            call("decide_card", card=small, decision=TAKE_BACK, said="undo it"),
            say("I can't do that."),
        ]
    )
    answer = ask(solo, "what's waiting on me?", model)
    assert answer.calls == [] and _status(solo, "card-small") == "approved"
    assert "own words" in model.calls[1].turns()[-1].content


# ---- words that come close: a button, and her tap takes it back (2026-11-19) ---------
# The owner's read of 2026-11-19: "take back my yes" works, "oops, I didn't mean
# that" didn't. Words that come close put up a Take back button under Tim's
# message; her own tap does it. Words that don't come close still never do.


@pytest.mark.parametrize(
    ("said", "close"),
    [
        ("oops, I didn't mean to approve that", True),
        ("I approved the August report by mistake", True),
        ("that was the wrong one", True),
        ("whoops", True),
        ("I shouldn't have said yes to that", True),
        ("what's waiting on me?", False),
        ("approve my August report", False),
        ("I paid for the oops-proof phone case", False),
    ],
)
def test_what_comes_close_to_taking_it_back(said, close):
    from core.tools.decide import comes_close_to_take_back

    assert comes_close_to_take_back(said) is close


def test_words_that_come_close_put_up_a_take_back_button(solo):  # noqa: F811
    voice_on(solo)
    small = _approve_august(solo)
    model = FixtureAdapter(
        [
            call("decide_card", card=small, decision=TAKE_BACK, said="undo"),
            say("Tap Yes and I'll take it back."),
        ]
    )
    answer = ask(solo, "oops, I didn't mean to approve that", model)
    assert answer.calls == [] and _status(solo, "card-small") == "approved"
    assert answer.waiting["kind"] == "confirm"
    assert answer.waiting["summary"].startswith("Take back your yes on ")
    assert "#" not in answer.waiting["summary"]  # whose it is and what for, never a number
    assert answer.waiting["args"]["said"] == "oops, I didn't mean to approve that"


def test_her_tap_on_the_button_takes_it_back(solo):  # noqa: F811
    from core.talk import Waiting, tapped

    voice_on(solo)
    small = _approve_august(solo)
    model = FixtureAdapter(
        [call("decide_card", card=small, decision=TAKE_BACK, said="undo"), say("Tap Yes.")]
    )
    ask(solo, "oops, I didn't mean to approve that", model)
    held = Waiting(solo["ledger_root"], "living-water", "daniel-reyes").shown()
    after = tapped(
        box(solo),
        held["id"],
        yes=True,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        adapter=FixtureAdapter([say("It's taken back.")]),
    )
    assert after is not None and after.say == "It's taken back."
    assert _status(solo, "card-small") == "pending"
    back = json.loads(next(c for c in books(solo)["cards"] if c[0] == small)[2])["taken_back"]
    [kept] = json.loads(back)
    assert kept["said"] == "oops, I didn't mean to approve that" and kept["via"] == "tap"


def test_not_now_on_the_button_leaves_her_yes_standing(solo):  # noqa: F811
    from core.talk import Waiting, tapped

    voice_on(solo)
    small = _approve_august(solo)
    model = FixtureAdapter(
        [call("decide_card", card=small, decision=TAKE_BACK, said="undo"), say("Tap Yes.")]
    )
    ask(solo, "oops, wrong one", model)
    held = Waiting(solo["ledger_root"], "living-water", "daniel-reyes").shown()
    tapped(
        box(solo),
        held["id"],
        yes=False,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )
    assert _status(solo, "card-small") == "approved"


def test_no_button_when_it_can_no_longer_be_taken_back(solo):  # noqa: F811
    voice_on(solo)
    small = _approve_august(solo)
    with Ledger.open(solo["ledger_root"]) as ledger:  # a run since her yes: it stands
        ledger.conn.execute("UPDATE runs SET created_at = '2099-01-01T00:00:00+00:00'")
        ledger.conn.commit()
    model = FixtureAdapter(
        [call("decide_card", card=small, decision=TAKE_BACK, said="undo"), say("It stands.")]
    )
    answer = ask(solo, "oops, I didn't mean to approve that", model)
    assert answer.waiting is None and _status(solo, "card-small") == "approved"
    assert "may already have been acted on" in model.calls[1].turns()[-1].content


def test_a_tap_is_the_only_way_past_her_words(solo):  # noqa: F811
    """The model can't reach the tap's path: decide_card as the model calls
    it, with words that come close, still waits for her tap."""
    voice_on(solo)
    small = _approve_august(solo)
    with pytest.raises(ValueError, match="own words"):
        box(solo).call("decide_card", {"card": small, "decision": TAKE_BACK, "said": "oops"})
    assert _status(solo, "card-small") == "approved"
