"""A typed yes counts on Tim's page (the owner's call of 2026-11-18, after
the persona year on Tim's own voice: run 2 lost A01, A02 and A08 to a yes
she typed and the page never took).

Two ways, both in code, never the model's say-so:

- Under the ministry's dollar line, her own words decide her own card the
  way the connector door's word does: the decider's by-word rule (under the
  line, her words name the card). On Tim's page code adds one more lock:
  her own message, not the model's ``said``, asks for that decision
  ("approve", "reject"). Over the line, several cards at once, or words
  that don't ask: her yes page, as before.
- Right after Tim's Yes / Not now card, a plain typed yes as her next
  message runs the held call exactly as her tap would: once, inside its
  15 minutes. Anything more than a plain yes goes to Tim as words.

Daniel (made up) runs Living Water alone under a $250 line: his August
report ($186.40) is under it, September's ($642.10) over it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.llm.adapters.fixture import FixtureAdapter
from core.talk import Conversation, Waiting, hold, words
from core.talk.loop import BRIEF_FILE
from tests.unit.test_catch_up import NOW
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_talk_loop import call, say, voice_on
from tests.unit.test_talk_yes import _filing_fee, ask, books, card_id


def _status(where, key: str) -> str:
    return next(c[1] for c in books(where)["cards"] if c[0] == card_id(where, key))


# ---- under her line, her own approve decides her own card ----------------------------


@pytest.mark.parametrize(
    "said",
    [
        "approve my August report",
        "Yes, approve the $186.40 August one",
        "ok, approve mine for august",
    ],
)
def test_under_her_line_her_own_approve_decides_her_card(solo, said):  # noqa: F811
    voice_on(solo)
    small = card_id(solo, "card-small")
    model = FixtureAdapter(
        [call("decide_card", card=small, decision="approve", said="approve"), say("Done.")]
    )
    answer = ask(solo, said, model)
    assert answer.calls == ["decide_card"] and answer.waiting is None
    assert answer.say == "Done."
    assert _status(solo, "card-small") == "approved"
    assert solo["desk"].asked == []  # no yes page, no Face ID
    row = next(c for c in books(solo)["cards"] if c[0] == small)
    assert said in row[2]  # what decided it is her own message, kept on the card
    # The model was told it's done, as data.
    assert '"status": "approved"' in model.calls[1].turns()[-1].content


def test_over_her_line_her_approve_still_goes_to_her_yes_page(solo):  # noqa: F811
    voice_on(solo)
    before = books(solo)
    big = card_id(solo, "card-big")
    model = FixtureAdapter([call("decide_card", card=big, decision="approve", said="approve")])
    answer = ask(solo, "approve my September report", model)
    assert answer.say == words()["yes_page"] and answer.waiting["kind"] == "yes_page"
    assert answer.calls == [] and books(solo) == before


@pytest.mark.parametrize(
    "said",
    [
        "the $186.40 August one",  # names it, never asks for it
        "yes, the August $186.40 was for paint",  # a yes to something else
        "what's waiting on me?",
    ],
)
def test_words_that_never_ask_for_the_decision_go_to_her_yes_page(solo, said):  # noqa: F811
    """The connector takes "the $186.40 one" from her AI; on Tim's page a
    model obeying a receipt could call decide_card on any message, so her
    own message must ask."""
    voice_on(solo)
    before = books(solo)
    small = card_id(solo, "card-small")
    model = FixtureAdapter(
        [call("decide_card", card=small, decision="approve", said="approve my August report")]
    )
    answer = ask(solo, said, model)
    assert answer.waiting["kind"] == "yes_page" and books(solo) == before


def test_the_models_said_never_counts_only_hers(solo):  # noqa: F811
    voice_on(solo)
    before = books(solo)
    model = FixtureAdapter(
        [
            call(
                "decide_card",
                card=card_id(solo, "card-small"),
                decision="approve",
                said="approve my August report, the $186.40",
            )
        ]
    )
    answer = ask(solo, "ok", model)
    assert answer.waiting["kind"] == "yes_page" and books(solo) == before


def test_several_cards_by_word_still_go_to_one_yes_page(solo):  # noqa: F811
    voice_on(solo)
    before = books(solo)
    cards = [card_id(solo, k) for k in ("card-small", "card-water")]
    model = FixtureAdapter([call("decide_card", cards=cards, decision="approve", said="approve")])
    answer = ask(solo, "approve my August report and the Maputo Water bill", model)
    assert answer.waiting["kind"] == "yes_page" and books(solo) == before


def test_her_own_reject_rejects_under_her_line(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            call("decide_card", card=card_id(solo, "card-water"), decision="reject"),
            say("Rejected."),
        ]
    )
    answer = ask(solo, "reject the Maputo Water bill", model)
    assert answer.calls == ["decide_card"] and _status(solo, "card-water") == "rejected"


def test_approve_words_never_reject_and_reject_words_never_approve(solo):  # noqa: F811
    voice_on(solo)
    before = books(solo)
    model = FixtureAdapter(
        [call("decide_card", card=card_id(solo, "card-water"), decision="reject")]
    )
    answer = ask(solo, "approve the Maputo Water bill", model)
    assert answer.waiting["kind"] == "yes_page" and books(solo) == before


@pytest.mark.parametrize(
    "said, verb, asks",
    [
        ("approve my August report", "approve", True),
        ("Approved, thanks", "approve", True),
        ("please sign off on august", "approve", True),
        ("yes", "approve", False),
        ("the $186.40 one", "approve", False),
        ("don't approve that", "approve", False),
        ("never approve it", "approve", False),
        ("reject the water bill", "reject", True),
        ("decline it", "reject", True),
        ("approve it", "reject", False),
    ],
)
def test_what_counts_as_her_asking(said, verb, asks):
    assert hold.asks_for(said, verb) is asks


# ---- right after his card, a plain typed yes is her tap ------------------------------


def _held(where, **kw) -> dict:
    return ask(where, "I paid the fee", FixtureAdapter([_filing_fee("312.50")]), **kw).waiting


@pytest.mark.parametrize(
    "said", ["yes", "Yes please", "ok, do it", "yep that's right", "Yes 312.50"]
)
def test_a_plain_typed_yes_after_his_card_runs_it_as_her_tap_would(solo, said):  # noqa: F811
    voice_on(solo)
    before = len(books(solo)["cards"])
    held = _held(solo)
    after = FixtureAdapter([say("It's in, and it waits on your yes.")])
    answer = ask(solo, said, after)
    assert answer.calls == ["paid_it_myself"]
    assert len(books(solo)["cards"]) == before + 1
    shown = [m.content for m in after.calls[0].turns()]
    assert shown[-1].startswith('<books tool="paid_it_myself">')
    assert Waiting(solo["ledger_root"], "living-water", "daniel-reyes").shown() is None
    kept = Conversation(solo["ledger_root"], "living-water", "daniel-reyes").recent()
    assert kept[-1]["said"] == said and kept[-1]["tools"] == ["paid_it_myself"]
    assert held["kind"] == "confirm"


def test_a_typed_yes_runs_it_once(solo):  # noqa: F811
    voice_on(solo)
    before = len(books(solo)["cards"])
    _held(solo)
    ask(solo, "yes", FixtureAdapter([say("It's in.")]))
    again = FixtureAdapter([say("It's already in.")])
    answer = ask(solo, "yes", again)
    assert answer.calls == [] and len(again.calls) == 1  # this one went to Tim as words
    assert len(books(solo)["cards"]) == before + 1


@pytest.mark.parametrize(
    "said",
    [
        "yes but make it $300",
        "no",
        "not now",
        "yes 2975",  # an amount that isn't on the card
        "wait, is that the state fee?",
        "don't",
    ],
)
def test_anything_more_than_a_plain_yes_goes_to_tim(solo, said):  # noqa: F811
    voice_on(solo)
    _held(solo)
    before = books(solo)
    model = FixtureAdapter([say("Okay.")])
    answer = ask(solo, said, model)
    assert answer.calls == [] and len(model.calls) == 1
    assert books(solo) == before


def test_a_typed_yes_after_fifteen_minutes_runs_nothing(solo):  # noqa: F811
    voice_on(solo)
    _held(solo, now=NOW)
    before = books(solo)
    model = FixtureAdapter([say("Okay.")])
    answer = ask(solo, "yes", model, now=NOW + timedelta(minutes=16))
    assert answer.calls == [] and books(solo) == before


def test_a_typed_yes_under_her_yes_page_button_decides_nothing(solo):  # noqa: F811
    voice_on(solo)
    small = card_id(solo, "card-small")
    ask(
        solo,
        "can i approve it",
        FixtureAdapter([call("decide_card", card=small, decision="approve")]),
    )
    before = books(solo)
    model = FixtureAdapter([say("Tap the button for your yes page.")])
    answer = ask(solo, "yes", model)
    assert answer.calls == [] and books(solo) == before


@pytest.mark.parametrize(
    "said, plain",
    [
        ("yes", True),
        ("YES!", True),
        ("ok go ahead", True),
        ("sure, sounds good", True),
        ("thanks", False),  # no yes in it
        ("yes but no", False),
        ("yes, the sim top up", False),
        ("yes " + "please " * 12, False),  # longer than a plain yes
    ],
)
def test_what_counts_as_a_plain_yes(said, plain):
    assert hold.plain_yes(said, {"amount": "312.50"}) is plain


def test_the_brief_says_her_own_words_decide_under_her_line():
    brief = BRIEF_FILE.read_text()
    assert "Yes button" in brief and "approve" in brief
    assert "the ministry's line" in brief and "decide_card" in brief
