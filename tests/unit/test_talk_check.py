"""What Tim's words may say, checked in code before she reads them (the
persona year on Tim's own voice, run 2, 2026-11-18, Haiku 5.5 as Tim).

- S07: "I can't see a tool here that moves a bank line onto your personal
  side. The closest one is paid_it_myself": a tool's name in his words,
  and a claim no tool could do it when explain_bank_line can.
- D01: "Done, I've put NPR 1,000.00 of ministry spending under Other",
  with nothing written that message.
- S02: "Yes, send the photo and I'll read it", with no add_receipt call,
  so no button to send it with.
- S01, and M04 in both runs: he took back things that WERE done ("I never
  changed the $500 from your mom"). His history named every earlier tool
  as ``read_from``, writes and all, so he couldn't tell a write stood.

Each check sends the model one note and lets it try again, once a message;
the history tells reads from writes.
"""

from __future__ import annotations

import json

import pytest

from core.llm.adapters.fixture import FixtureAdapter
from core.talk import check, hold
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_talk_loop import call, say, voice_on
from tests.unit.test_talk_yes import _filing_fee, ask, card_id, tap

FEE = "I paid the $312.50 state filing fee myself on Oct 12"


def _shown(model, n: int) -> list[str]:
    return [m.content for m in model.calls[n].turns()]


# ---- a tool's name never reaches her ---------------------------------------------------


def test_a_tool_name_in_his_words_goes_back_for_plain_words(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            say("No tool moves a bank line. The closest one is paid_it_myself, which doesn't fit."),
            say("I can mark that line as yours, so you owe it back. Want me to?"),
        ]
    )
    answer = ask(solo, "the $96.40 guest house on June 22 was mine", model)
    assert answer.say == "I can mark that line as yours, so you owe it back. Want me to?"
    note = _shown(model, 1)[-1]
    assert note.startswith("<note>") and "paid_it_myself" in note
    assert "never appear in your words" in note and "call it" in note


def test_the_check_comes_once_a_message(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say("Try paid_it_myself."), say("Still paid_it_myself.")])
    answer = ask(solo, "what do I use?", model)
    assert len(model.calls) == 2 and answer.say == "Still paid_it_myself."


def test_no_check_on_the_last_step(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say("Try paid_it_myself.")])
    answer = ask(solo, "what do I use?", model, max_steps=1)
    assert answer.status == "ok" and len(model.calls) == 1


# ---- "done" only when something was written --------------------------------------------


def test_done_with_nothing_written_goes_back(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            call("month_report", month="2026-10"),
            say("Done, I've put NPR 1,000.00 of ministry spending under Other."),
            say("I haven't recorded the 1,000 rupees yet. Say the word and I will."),
        ]
    )
    answer = ask(solo, "the teachers' tea and taxis were 1,000 rupees", model)
    assert answer.say.startswith("I haven't recorded the 1,000 rupees yet.")
    assert "nothing was written" in _shown(model, 2)[-1]


def test_done_after_a_write_ran_stands(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([_filing_fee("312.50"), say("Done. I've recorded the fee.")])
    answer = ask(solo, FEE, model)
    assert answer.say == "Done. I've recorded the fee." and len(model.calls) == 2


def test_done_after_her_tap_stands(solo):  # noqa: F811
    voice_on(solo)
    held = ask(solo, "I paid the fee", FixtureAdapter([_filing_fee("312.50")])).waiting
    after = FixtureAdapter([say("I've recorded it, and it waits on your yes.")])
    answer = tap(solo, held["id"], model=after)
    assert answer.say == "I've recorded it, and it waits on your yes." and len(after.calls) == 1


@pytest.mark.parametrize(
    "words, claims",
    [
        ("Done. The $500 from your mom is now marked as her personal gift.", True),
        ("Done, I've put NPR 1,000.00 of ministry spending under Other.", True),
        ("I've recorded the $18.00 SIM top-up for the school phone.", True),
        ("I've already recorded it.", True),
        ("I have marked it as yours.", True),
        ("I haven't recorded anything for it.", False),
        ("Once it's recorded, you'll see it on your page.", False),
        ("I can record that as ministry spending once you confirm it.", False),
        ("Nothing has been recorded against it.", False),
        ("Tell me in a few words and I'll record it.", False),
        ("I've checked your tax packet.", False),
        ("Not done yet: tap Yes and it is.", False),
    ],
)
def test_what_counts_as_saying_its_done(words, claims):
    assert check.claims_done(words) is claims


@pytest.mark.parametrize(
    "words, so",
    [
        ("September is reviewed and approved.", True),
        ("Your filing fee is now recorded.", True),
        ("Okay, it's kept.", True),
        ("Nothing has been recorded against it.", False),
        ("Once it's recorded, you'll see it on your page.", False),
        ("It isn't recorded until you tap Yes.", False),
        ("Your September is in.", False),
        ("Tap Yes to record the fee.", False),
    ],
)
def test_what_counts_as_saying_a_change_stands(words, so):
    assert check.says_its_so(words) is so


# ---- asking for a photo puts up its button ---------------------------------------------


def test_asking_for_the_photo_with_no_button_goes_back_for_the_button(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            say("Yes, send the photo and I'll read it."),
            call("add_receipt", said="notebooks and chalk, $12.40"),
            say("Tap the button under my message and take the picture."),
        ]
    )
    answer = ask(solo, "bought notebooks and chalk today, $12.40. i'll send the photo", model)
    assert answer.calls == ["add_receipt"] and answer.waiting["kind"] == "link"
    assert "add_receipt" in _shown(model, 1)[-1]


def test_asking_for_the_photo_under_its_button_stands(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            call("add_receipt", said="notebooks and chalk, $12.40"),
            say("Send the photo with the button under my message."),
        ]
    )
    answer = ask(solo, "bought notebooks and chalk today, $12.40", model)
    assert len(model.calls) == 2 and answer.waiting["kind"] == "link"


@pytest.mark.parametrize(
    "words, asks",
    [
        ("Yes, send the photo and I'll read it.", True),
        ("Please take a picture of the receipt.", True),
        ("Send that photo again when you can.", True),
        ("There's no need to send the photo again.", False),
        ("I already have the photo.", False),
        ("Your September report is waiting on your yes.", False),
    ],
)
def test_what_counts_as_asking_for_a_photo(words, asks):
    assert check.asks_for_photo(words) is asks


# ---- a button he points to is a button on her page (V4, Haiku run, 2026-11-18) ----------
#
# A02: "Tap the approval button on your yes page and finish with Face ID", with
# no decide_card call, so nothing under his message to tap; her own words
# ("yes, approve the $64.00 reimbursement"), under the ministry's line, would
# have decided it.


def test_pointing_to_a_button_that_isnt_there_goes_back_for_the_call(solo):  # noqa: F811
    voice_on(solo)
    small = card_id(solo, "card-small")
    model = FixtureAdapter(
        [
            say("Tap the approval button on your yes page and finish with Face ID."),
            call(
                "decide_card", card=small, decision="approve", said="can i just approve it myself"
            ),
        ]
    )
    answer = ask(solo, "can i just approve it myself", model)
    assert answer.waiting["kind"] == "yes_page"
    told = _shown(model, 1)[-1]
    assert told.startswith("<note>") and "decide_card" in told


def test_a_card_number_on_his_second_try_goes_back_again(solo):  # noqa: F811
    """The Haiku year's A01, 2026-11-19: the first try drew a note, and the
    second, which went out unchecked, said "card 3". A number, or a button
    that isn't there, gets one more try; nothing else does."""
    voice_on(solo)
    model = FixtureAdapter(
        [
            say("Try paid_it_myself."),
            say("The renewal is card 3 in your list."),
            say("The $312.50 renewal is waiting on your yes."),
        ]
    )
    answer = ask(solo, "what about the renewal?", model)
    assert len(model.calls) == 3 and answer.say == "The $312.50 renewal is waiting on your yes."
    assert "by its number (card 3)" in _shown(model, 2)[-1]


def test_a_button_that_isnt_there_on_his_second_try_goes_back_again(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            say("Try paid_it_myself."),
            say("Your page shows the yes button for it. Tap it yourself."),
            say("Say approve it and I'll put up your yes button."),
        ]
    )
    answer = ask(solo, "what about the renewal?", model)
    assert len(model.calls) == 3 and answer.say.startswith("Say approve it")


def test_no_more_than_two_tries_go_back(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [say("Try paid_it_myself."), say("It's card 3."), say("It's card 3."), say("unused")]
    )
    answer = ask(solo, "what about the renewal?", model)
    assert len(model.calls) == 3 and answer.say == "It's card 3."


@pytest.mark.parametrize(
    "words, points",
    [
        (
            "That's your yes to give, Eli. Tap the approval button on your yes page and finish "
            "with Face ID or your fingerprint, and it's done.",
            True,
        ),
        ("Tap the button below.", True),
        ("Open your yes page and use Face ID.", True),
        (
            "Your yes page will ask for Face ID or your fingerprint, and it won't count as "
            "approved until you finish there.",
            True,
        ),
        # the Haiku year's A01, 2026-11-19: no button was under it
        ("Your page shows the yes button for it. Tap it yourself.", True),
        ("Tap it with Face ID.", True),
        ("There's no need to tap anything.", False),
        ("No yes button is needed for this one.", False),
        ("Your receipt page and your month page work the same as always.", False),
        ("Martin approves it once he's looked it over.", False),
        ("Tell me once you've counted the cash.", False),
    ],
)
def test_what_counts_as_pointing_to_a_button(words, points):
    assert check.points_to_a_button(words) is points


# ---- his history tells what he read from what he wrote ----------------------------------


def test_a_past_reply_shows_what_it_wrote_apart_from_what_it_read(solo):  # noqa: F811
    voice_on(solo)
    ask(solo, FEE, FixtureAdapter([call("waiting_cards"), _filing_fee("312.50"), say("It's in.")]))
    model = FixtureAdapter([say("Yes, it's in.")])
    ask(solo, "did the fee go in?", model)
    past = json.loads(_shown(model, 0)[1])
    fresh = past.pop("books_now")  # the read behind it, run again (2026-11-20); the write never
    assert past == {"say": "It's in.", "read_from": ["waiting_cards"], "wrote": ["paid_it_myself"]}
    assert [r["tool"] for r in fresh] == ["waiting_cards"]


def test_a_past_reply_that_only_wrote_has_no_read_from(solo):  # noqa: F811
    voice_on(solo)
    ask(solo, FEE, FixtureAdapter([_filing_fee("312.50"), say("It's in.")]))
    model = FixtureAdapter([say("Yes.")])
    ask(solo, "did it go in?", model)
    assert json.loads(_shown(model, 0)[1]) == {"say": "It's in.", "wrote": ["paid_it_myself"]}


def test_the_brief_says_what_wrote_means():
    from core.talk.loop import BRIEF_FILE

    assert "`wrote`" in BRIEF_FILE.read_text()


# ---- the bank line tool says it takes a line with a receipt too ------------------------


def test_explain_bank_line_says_it_takes_her_own_charge_with_a_receipt():
    from core.tools.bank import EXPLAIN_BANK_LINE

    short = EXPLAIN_BANK_LINE.short
    assert "with no receipt" not in short.split(":")[0]
    assert "receipt or not" in short and "card" in short


# ---- he answers what she asked and holds what he noticed (the owner's call, 2026-11-18) --
#
# D06 (the Haiku run): Eli asked whether his jeep receipt went in. Tim said yes,
# then "I also need to flag something" about a cash line, and the beat turned
# into that. Words in the brief moved nothing (0 of 5 on Haiku); this note, on
# his first try, held it in 9 of 10.

D06 = (
    "The jeep receipt is in the books: NPR 850.00, under Travel. I also need to flag "
    "something. The books show a cash line of NPR 1,500.00 for tea and taxis, and you told "
    "me it was 1,000."
)


@pytest.mark.parametrize(
    "words, tacked",
    [
        (D06, True),
        ("Yes, it's in. I noticed the taxi on the 17th has no receipt.", True),
        ("It's in under Travel. One more thing: September still waits on your yes.", True),
        ("Yes, it's in. I also need to correct my last reply: the tea is your own cash.", True),
        ("Yes, the jeep is in under Travel, so no need to send the photo again.", False),
        ("I noticed two lines that don't match: the tea is 1,500 and you said 1,000.", False),
        ("You have one thing waiting: your September report.", False),
    ],
)
def test_what_counts_as_tacking_on_something_she_didnt_ask(words, tacked):
    assert check.tacks_on(words) is tacked


def test_something_she_didnt_ask_about_goes_back_once(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say(D06), say("Yes, the jeep receipt is in, under Travel.")])
    answer = ask(solo, "did my jeep receipt go in?", model)
    assert answer.say == "Yes, the jeep receipt is in, under Travel."
    told = _shown(model, 1)[-1]
    assert told.startswith("<note>") and "what's waiting" in told


def test_the_tacked_on_note_comes_after_the_others():
    note = check.problem(
        "Tap the button below. I also need to flag the taxi.",
        tools=[],
        wrote=False,
        button=False,
    )
    assert "nothing is under your message" in note


# ---- his words agree with the yes button that came back (tonight's Haiku run, A01) -----
#
# Code put Eli's yes button back under Tim's reply (engine #699) and Eli tapped
# it, and it went through; but Tim's words beside it said "I can't send a new
# link from here", and the beat ran past its turns. The note, on his first
# try, had Haiku say the button is there 5 of 5.

NO_LINK = (
    "I can't send a new link from here, Eli, and I haven't approved anything for you. Open "
    "the approval again from your email or your page."
)


@pytest.mark.parametrize(
    "words, denies",
    [
        (NO_LINK, True),
        ("Sorry, I'm unable to create a fresh link for that.", True),
        ("There's no new button for it.", True),
        ("I can't send money, but your yes button is below.", False),
        ("Your yes button is under this message. Tap it and it's done.", False),
    ],
)
def test_what_counts_as_denying_the_yes_button(words, denies):
    assert check.denies_the_button(words) is denies


def test_with_her_yes_button_back_words_that_deny_it_go_back(solo):  # noqa: F811
    from tests.unit.test_talk_yes import MINUTE, NOW, _ran_out

    voice_on(solo)
    _ran_out(solo, card_id(solo, "card-small"))
    model = FixtureAdapter([say(NO_LINK), say("A fresh yes button is under this message.")])
    answer = ask(solo, "the link expired, can you send a new one?", model, now=NOW + 5 * MINUTE)
    assert answer.say == "A fresh yes button is under this message."
    assert answer.waiting["kind"] == "yes_page"
    told = _shown(model, 1)[-1]
    assert told.startswith("<note>") and "fresh yes button is under your message" in told


def test_without_a_button_back_the_words_are_left_alone():
    assert check.problem(NO_LINK, tools=[], wrote=False, button=False) == ""


# ---- her plain yes to a decision, never left unanswered (the owner, 2026-11-19) -------
# On T2T, the first real use of Tim's own voice: "It's approved." Tim only
# looked, then said "Tap Yes on your page", with nothing on the page to tap.


@pytest.mark.parametrize(
    ("said", "verb"),
    [
        ("It's approved.", "approve"),
        ("approved", "approve"),
        ("I approve it", "approve"),
        ("yes, approve it please", "approve"),
        ("ok sign off on it", "approve"),
        ("reject it", "reject"),
        ("it's declined", "reject"),
        ("my visa got approved", ""),
        ("is it approved?", ""),
        ("it's not approved", ""),
        ("approve my August report and the September one too", ""),
        ("yes", ""),
    ],
)
def test_a_plain_decision_is_her_whole_message(said, verb):
    assert hold.plain_decision(said) == verb


def test_her_plain_yes_to_a_decision_goes_back_when_nothing_acted_on_it():
    note = check.problem(
        "It still shows waiting on your yes.", tools=[], wrote=False, button=False, asked="approve"
    )
    assert "decide_card" in note and "Never mention this note" in note
    for kw in ({"button": True}, {"held": True}, {"decided": True}):
        base = {"button": False, "held": False, "decided": False} | kw
        assert check.problem("It's waiting.", tools=[], wrote=False, asked="approve", **base) == ""


@pytest.mark.parametrize(
    "words",
    ["Tap Yes on your page to approve it.", "Just tap yes and it's done.", "tap the Yes below"],
)
def test_tap_yes_points_to_a_button(words):
    assert check.points_to_a_button(words)
    assert check.problem(words, tools=[], wrote=False, button=False)


def test_its_approved_puts_up_her_yes_instead_of_pointing_at_nothing(solo):  # noqa: F811
    """The T2T exchange of 2026-11-19, over the ministry's line: her "It's
    approved." gets her yes button, never "tap Yes on your page"."""
    voice_on(solo)
    big = card_id(solo, "card-big")
    model = FixtureAdapter(
        [
            call("waiting_cards"),
            say("It still shows waiting on your yes. Tap Yes on your page to approve it."),
            call("decide_card", card=big, decision="approve", said="It's approved."),
        ]
    )
    answer = ask(solo, "It's approved.", model)
    assert answer.waiting is not None and answer.waiting["kind"] == "yes_page"
    assert "decide_card" in model.calls[2].turns()[-1].content  # the note, once


# ---- a card's number never reaches her (the Haiku run of 2026-11-18, A09) ---------------
#
# Tim said "The September report is card 1 ... a $64.00 printer cartridge card
# (card 3) ... Do you want me to approve card 1?", and Eli typed back "approve
# card 1": the number the brief keeps in his calls had become her words.

A09 = (
    "The September report is card 1, an expense report for you, $0.00, and it's yours to "
    "approve. A $64.00 printer cartridge card (card 3) is also waiting. Do you want me to "
    "approve card 1?"
)


@pytest.mark.parametrize(
    "words, named",
    [
        (A09, True),
        ("That's card #10, the Sunbiz fee.", True),
        ("The Sunbiz fee (#10) waits on your yes.", True),
        ("Cards 1 and 3 wait on you.", True),
        ("Record 12 is the taxi.", True),
        ("It's item number 4 on your list.", True),
        ("The $64.00 printer cartridges wait on your yes.", False),
        ("The $312.50 fee is approved.", False),
        ("The taxi is dated November 17, 2026.", False),
        ("Eli paid $64.00 on 2026-11-12 with his own card.", False),
        ("The charge on your card on 11/12 is the guest house.", False),
        ("Check #1351 cleared on the 14th.", False),
        ("Invoice #2043 is in the books.", False),
        ("Your card ending 4821 paid it.", False),
        ("The 950 rupee taxi is in, and 1 more thing waits.", False),
    ],
)
def test_what_counts_as_naming_a_card_by_its_number(words, named):
    assert bool(check.card_number(words)) is named


def test_a_card_number_in_his_words_goes_back_for_what_it_is(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(
        [
            say(A09),
            say("The September report is yours to approve. Do you want me to approve it?"),
        ]
    )
    answer = ask(solo, "can I just approve the september report myself?", model)
    assert answer.say == "The September report is yours to approve. Do you want me to approve it?"
    note = _shown(model, 1)[-1]
    assert note.startswith("<note>") and "card 1" in note and "what for" in note


def test_the_card_number_note_names_no_money_or_dates():
    assert (
        check.problem("Yes, the $64.00 cartridges from 11/12.", tools=[], wrote=False, button=False)
        == ""
    )


# ---- an unasked "Also," after the answer (the Haiku run of 2026-11-18, D01) -------------
#
# Eli asked one thing (did the taxi go in?) and Tim answered it, then "Also, the
# books now show the $18.00 SIM top-up and the $26.00 whiteboard as approved
# ...". At "that's all I needed" he answered "Before you go, two things to
# check ... Also, ...". An "also" that answers the second half of her own
# question stands.

D01 = (
    "Yes, it's in the books. The 950 rupee taxi receipt is recorded under Travel at $7.09. "
    "Also, the books now show the $18.00 SIM top-up and the $26.00 whiteboard as approved, "
    "which doesn't match what I told you earlier."
)
D01_ASKED = (
    "hey tim, the app says you're disconnected. did the blurry taxi receipt from yesterday, "
    "the 950 rupees one, go through before that happened? i want to know if its in the books"
)


@pytest.mark.parametrize(
    "words, said, tacked",
    [
        (D01, D01_ASKED, True),
        (D01, "", True),
        (
            "You're welcome, Eli. Before you go, two things to check: the SIM and the whiteboard.",
            "Thanks, that's all I needed.",
            True,
        ),
        (
            "Yes, the taxi is in. Also, September went for review on the 3rd.",
            "did the taxi go in, and is september sent?",
            False,
        ),
        (
            "Yes, the taxi is in. Also, the bus is under Travel.",
            "did the taxi go in? what about the bus?",
            False,
        ),
        (
            "Here's a fresh button. Also, travel is now 900.",
            "it expired. also make travel 900",
            False,
        ),
        (
            "Done, it's dated the 17th. Also, it's under Travel now.",
            "fix the date to the 17th and move it to travel",
            False,
        ),
        ("Yes, it's in. It's also under Travel, like the bus.", "did the taxi go in?", False),
    ],
)
def test_what_counts_as_an_unasked_also(words, said, tacked):
    assert check.tacks_on(words, said=said) is tacked


def test_an_unasked_also_goes_back_once(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say(D01), say("Yes, it's in the books under Travel.")])
    answer = ask(solo, D01_ASKED, model)
    assert answer.say == "Yes, it's in the books under Travel."
    told = _shown(model, 1)[-1]
    assert told.startswith("<note>") and "what's waiting" in told


def test_an_also_that_answers_her_second_question_stands(solo):  # noqa: F811
    voice_on(solo)
    words = "The taxi is in. Also, September went for review on the 3rd."
    model = FixtureAdapter([say(words)])
    answer = ask(solo, "did the taxi go in, and is september sent?", model)
    assert answer.say == words and len(model.calls) == 1


# ---- whose yes it is (the Haiku year, A09, 2026-11-19) -------------------------------
#
# Eli asked whether his September report was his to approve; nobody else holds
# approval in his ministry, so it is (the owner, 2026-11-18). Tim said "It isn't
# yours to approve, Eli. Your September report goes to your board member", and,
# after reading the waiting list that said it was his, "Your board member decides
# both." Words that say a card isn't hers go back when code finds it waits on her
# own yes.

A09_FIRST = (
    "It isn't yours to approve, Eli. Your September report goes to your board member, "
    "and only their yes finishes it."
)
A09_LIST = "Two cards are waiting. Your board member decides both."


@pytest.mark.parametrize(
    ("words", "denies"),
    [
        (A09_FIRST, True),
        (A09_LIST, True),
        ("That one isn't yours to give: Martin approves it.", True),
        ("It's not your yes to give.", True),
        # the Haiku year on 42661c1, A09 again, 2026-11-19
        ("The September report needs the board's approval.", True),
        ("I know you said the board has to approve it.", True),
        ("It needs your reviewer's yes.", True),
        ("Your board member's approval is what finishes it.", True),
        ("It needs your yes.", False),
        ("It's yours to approve, Eli.", False),
        ("Your board member looks your month over after you approve it.", False),
        ("Nothing is waiting on your board member.", False),
    ],
)
def test_what_says_a_card_isnt_hers(words, denies):
    assert check.denies_hers(words) is denies


def test_saying_its_not_hers_goes_back_when_it_waits_on_her_yes(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say(A09_FIRST), say("It's yours to approve. Say the word.")])
    answer = ask(solo, "is my august report mine to approve, or my reviewer's?", model)
    assert answer.say == "It's yours to approve. Say the word."
    note = _shown(model, 1)[-1]
    assert note.startswith("<note>") and "waits on their own yes" in note
    assert "Daniel Reyes, August 2026" in note  # in her terms, never a number


def test_saying_its_not_hers_stands_when_nothing_waits_on_her(solo):  # noqa: F811
    import sqlite3
    from pathlib import Path

    voice_on(solo)
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    with con:
        con.execute("UPDATE approval_queue SET status = 'approved'")
    con.close()
    model = FixtureAdapter([say("It isn't yours to approve: your reviewer decides it.")])
    ask(solo, "can I approve my own report?", model)
    assert len(model.calls) == 1
