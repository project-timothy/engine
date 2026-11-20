"""What Tim's words may say, checked in code before she reads them (the
persona year on Tim's own voice, run 2, 2026-11-18, Haiku 5.5 as Tim).

What a model said that her page then showed her as true:

- a tool's name (S07: "The closest one is paid_it_myself"), which the
  brief already keeps out of his words;
- that he did something when nothing was written that message (D01:
  "Done, I've put NPR 1,000.00 under Other", after one read);
- asking her for a photo with no button to send it with (S02: "send the
  photo and I'll read it", with no ``add_receipt`` call);
- sending her to a button or her yes page with nothing under his message
  (the Haiku run, A02: "Tap the approval button on your yes page and finish
  with Face ID", with no ``decide_card`` call);
- after answering what she asked, bringing up something else in her books
  she didn't ask about (the Haiku run, D06: "The jeep receipt is in the
  books ... I also need to flag something", and the beat turned into that).
  The owner's call of 2026-11-18: Tim holds it for when she asks what's
  waiting, the way he holds the next item;
- saying he can't send a new link while code has put her yes button back
  under his message (the Haiku run of 2026-11-18, A01: "I can't send a new
  link from here", with the fresh button under it; she tapped it and it
  went through);
- naming a card by its number (the same run, A09: "The September report is
  card 1 ... Do you want me to approve card 1?", and Eli typed back "approve
  card 1"), which the brief keeps in his calls;
- an "Also, ..." after the answer to a one-part question (the same run,
  D01: "Also, the books now show the $18.00 SIM top-up ... as approved"),
  or "Before you go, two things to check" after "that's all I needed".

:func:`problem` names the first one found, as a note for the model (the
loop sends it and lets the model answer again, once a message). Nothing
here rewrites his words: the model says it again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PHOTO_TOOL = "add_receipt"

_DID = (
    r"recorded|marked|put|added|moved|changed|set|filed|counted|approved|rejected|saved|noted|"
    r"fixed|updated|kept|split|entered|logged|corrected|sent"
)
_CLAIMS = (
    re.compile(r"(?:^|[.!?]\s+)done\b"),  # "Done." / "Done, I've put ..."
    re.compile(rf"\b(?:i'?ve|i have)\s+(?:just\s+|already\s+|now\s+)?(?:{_DID})\b"),
)
_SO = re.compile(  # "September is reviewed and approved", "it's recorded" (V4 run 3)
    rf"\b(?:is|are|was|were|'s|been)\s+(?:now\s+|all\s+|already\s+)?(?:{_DID}|reviewed)\b"
)
_NOT_YET = re.compile(
    r"\b(?:not|nothing|never|no|once|when|until|after|if|unless)\b(?:\s+\S+){0,3}\s*$"
)
_PHOTO = re.compile(
    r"\b(?:send|take|snap|upload|share)\b(?:\s+\w+){0,4}?\s+(?:photo|picture|pic)s?\b"
)
_BUTTON = re.compile(  # "Tap the approval button on your yes page" (V4 A02)
    r"\b(?:tap\b(?:\s+\S+){0,4}?\s+(?:button|link|yes page)\b|your yes page\b|"
    r"the button (?:below|under)|tap\s+(?:the\s+)?yes\b|"  # "Tap Yes on your page" (T2T, 11/19)
    r"\byes button\b|\btap (?:it|that|them)\b)"  # "shows the yes button ... Tap it" (A01, 11/19)
)
_NO = re.compile(r"\b(?:no need|don'?t|do not|never|no)\b(?:\s+\w+){0,3}\s*$")


_TACKED = re.compile(  # "I also need to flag something" (the Haiku run, D06)
    r"\bi (?:also )?(?:need|want|have) to (?:flag|correct|mention|point out|raise)\b|"
    r"\bi (?:also )?noticed\b|\bone (?:more|other|last) thing\b|\banother thing\b|"
    r"\bsomething else (?:i noticed|to flag|in your books)\b|"
    r"\bbefore you go\b"  # "Before you go, two things to check" (the Haiku run, D01)
)
_ALSO = re.compile(r"(?:^|[.!?:]\s+)also\b")  # "Also, the books now show ..." (D01)
_ASKS = (  # words that start a question or a request in her message
    r"did|do|does|is|are|was|were|can|could|will|would|should|may|have|has|what|when|where|"
    r"which|who|whose|why|how|fix|change|move|make|put|set|add|mark|approve|reject|send|tell|"
    r"show|check|record|split|keep|take|give|let|please"
)
_HALVES = re.compile(rf";|,?\s+(?:and|plus|then)\s+(?=(?:{_ASKS})\b)")


_CARD_NO = re.compile(  # "card 1", "card #10", "cards 1 and 3", "record 12" (the Haiku run, A09)
    r"\b(?:card|record|item|request)s?\s+(?:#\s*|no\.\s*|number\s+)?\d+\b(?![/.:-]\d)|"
    r"(?<![\w$])#\d+\b"
)
_DOC_NO = re.compile(  # a check's or an invoice's own number is hers to hear
    r"\b(?:check|cheque|invoice|receipt|order|ticket|bill|po|ref|reference|room|flight|"
    r"confirmation)\s*$"
)


_NO_LINK = re.compile(  # "I can't send a new link from here" (the Haiku run, A01)
    r"\b(?:can'?t|cannot|can not|unable to)\s+(?:\S+\s+){0,3}?"
    r"(?:send|make|give|create|put up|set up|resend|get)\s+(?:[a-z'-]+\s+){0,3}?(?:link|button)\b|"
    r"\bno (?:new|fresh) (?:link|button)\b"
)


def _plain(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").split())


def tool_named(words: str, tools: Iterable[str]) -> str:
    """The first tool whose name (one no person types: it has an
    underscore) is in his words, or ""."""
    text = words.casefold()
    for name in sorted(tools):
        if "_" in name and re.search(rf"\b{re.escape(name.casefold())}\b", text):
            return name
    return ""


def claims_done(words: str) -> bool:
    """Whether his words say he did something to the books: "Done", "I've
    recorded", "I've already marked". A plan ("I'll record it"), a
    condition ("once it's recorded"), or a no ("I haven't recorded") is
    not a claim."""
    text = _plain(words)
    return any(claim.search(text) for claim in _CLAIMS)


def says_its_so(words: str) -> bool:
    """Whether his words say a change stands, the passive way: "September
    is reviewed and approved", "it's recorded". A no ("nothing has been
    recorded") or a condition ("once it's recorded") is not."""
    text = _plain(words)
    return any(not _NOT_YET.search(text[: found.start()]) for found in _SO.finditer(text))


def asks_for_photo(words: str) -> bool:
    """Whether his words ask her to send or take a photo, and not "no need
    to send the photo"."""
    text = _plain(words)
    return any(not _NO.search(text[: found.start()]) for found in _PHOTO.finditer(text))


def points_to_a_button(words: str) -> bool:
    """Whether his words send her to a button or her yes page ("tap the
    button below", "tap the approval button on your yes page", "your yes
    page will ask for Face ID"), and not "no need to tap anything"."""
    text = _plain(words)
    return any(not _NO.search(text[: found.start()]) for found in _BUTTON.finditer(text))


def asks_two(said: str) -> bool:
    """Whether her message asks two things ("did the taxi go in, and is
    September sent?", "fix the date and move it to travel", "also make
    travel 900"), so an "Also" in his answer answers her."""
    text = _plain(said)
    if re.search(r"\balso\b", text) or text.count("?") >= 2:
        return True
    return len([part for part in _HALVES.split(text) if part.strip()]) >= 2


def tacks_on(words: str, *, said: str = "") -> bool:
    """Whether his words, after an answer, bring up something else he noticed
    ("I also need to flag something", "I noticed the taxi has no receipt",
    "one more thing", "Before you go, two things to check"), or start
    another sentence with "Also" when her message (``said``) asked one
    thing: it waits until she asks what's waiting."""
    text = _plain(words)
    found = _TACKED.search(text)
    if found is not None and found.start() > 0:
        return True
    also = _ALSO.search(text)
    return also is not None and also.start() > 0 and not asks_two(said)


def card_number(words: str) -> str:
    """The first place his words name a card, a record, or an item by its
    number ("card 1", "card #10", "(#10)"), or "". Money, dates, a card's
    last four ("ending 4821"), and a check's or an invoice's own number are
    not one."""
    text = _plain(words)
    for found in _CARD_NO.finditer(text):
        if found.group().startswith("#") and _DOC_NO.search(text[: found.start()]):
            continue
        return found.group()
    return ""


def denies_the_button(words: str) -> bool:
    """Whether his words say he can't give her a new link or button ("I
    can't send a new link from here", "there's no new button for it")."""
    return _NO_LINK.search(_plain(words)) is not None


_NOT_HERS = re.compile(  # "It isn't yours to approve" / "Your board member decides both" (A09)
    r"\b(?:isn'?t|is not|not)\s+(?:yours|your\s+(?:yes|call|decision))(?:\s+to\s+\w+)?\b|"
    r"\b(?:your|the)\s+(?:board member|board|reviewer)(?:'s)?\s+(?:decides|approves|has to|must|"
    r"needs to|is the one|approval|yes)\b|\bgoes to your (?:board member|reviewer)\b|"
    # "needs the board's approval" (the Haiku year on 42661c1, A09 again)
    r"\bonly (?:their|your (?:board member|reviewer)'?s?) yes\b"
)


def denies_hers(words: str) -> bool:
    """Whether his words say a card isn't hers to decide ("it isn't yours
    to approve", "your board member decides both")."""
    return bool(_NOT_HERS.search(_plain(words)))


def hers_note(about: str) -> str:
    """The note when his words say it isn't hers, and code finds ``about``
    waiting on her own yes (the Haiku year's A09, 2026-11-19)."""
    return (
        f"Not sent yet: your words say it isn't theirs to decide, but the books say {about} "
        "waits on their own yes: their ministry lets them decide it. Your earlier words never "
        "reached them, so don't correct them; write your reply to them again: it's theirs. "
        "If they asked to approve or reject it, call decide_card with their words. Never "
        "mention this note."
    )


def _number_note(words: str) -> str:
    number = card_number(words)
    if not number:
        return ""
    return (
        f"Not sent yet: your words name something by its number ({number}). Numbers like "
        "that stay in your calls and never reach them: name it by who, what for, and how "
        "much, like the September report or the $64.00 printer cartridges. Write your "
        "reply to them again. Never mention this note."
    )


def _button_note(words: str, *, button: bool, held: bool) -> str:
    if not points_to_a_button(words) or button or held:
        return ""
    return (
        "Your words send them to a button or their yes page, but nothing is under your "
        "message. If they asked to approve or reject something, call decide_card with "
        "their words: under the ministry's line their word decides it, and otherwise its "
        "button shows under your message. If not, say it without sending them to tap."
    )


def misleads(words: str, *, button: bool, held: bool = False) -> str:
    """The note for the two faults that get a second try in a message (the
    Haiku year's A01, 2026-11-19: the redo, unchecked, said "card 3" and
    sent her to a yes button that wasn't there): a card's number in his
    words, or a button pointed to when none is under his message. ""."""
    return _number_note(words) or _button_note(words, button=button, held=held)


def problem(
    words: str,
    *,
    tools: Iterable[str],
    wrote: bool,
    button: bool,
    held: bool = False,
    yes_back: bool = False,
    asked: str = "",
    decided: bool = False,
    said: str = "",
) -> str:
    """The note for the model when his words would mislead her, or "".
    ``tools``: his tools' names; ``wrote``: a tool wrote to her books this
    message; ``button``: a button for her already waits under his
    message; ``held``: a call waits for her tap on the Yes under it;
    ``yes_back``: code put her yes button back under it this message;
    ``asked``: her whole message was a plain "approve" or "reject"
    (:func:`core.talk.hold.plain_decision`); ``decided``: decide_card ran
    this message; ``said``: her whole message, so an "Also" that answers
    the second half of her question stands."""
    names = list(tools)
    named = tool_named(words, names)
    if named:
        return (
            f"Your words named {named}, one of your tools. Tool names never appear in your "
            "words: say it again in plain words. If one of your tools does what they asked, "
            "call it instead of saying none can."
        )
    number = _number_note(words)
    if number:
        return number
    if yes_back and denies_the_button(words):
        return (
            "Your words say you can't send a new link, but a fresh yes button is under your "
            "message now. Write your reply to them again: say the button is there for them to "
            "tap, and nothing about not being able to send one. Never mention this note."
        )
    if asked and not decided and not button and not held:
        return (
            f"Not sent yet: their message says {asked} to a decision, and nothing acted on it: "
            "decide_card didn't run this message, and nothing waits under your message for "
            "them. If one card waits on their own decision, call decide_card for it with their "
            "words; if it isn't clear which card they mean, ask which. Never mention this note."
        )
    if held and not wrote and (claims_done(words) or says_its_so(words)):
        return (
            "Your words say it's done, but it waits for their tap on the Yes under your "
            "message, and nothing was written this message. Say plainly that their Yes does it."
        )
    if claims_done(words) and not wrote:
        return (
            "Your words say you did something, but nothing was written to their books this "
            "message. If they asked for it, call the tool now. If a tool shows it was done "
            "before, say that. Otherwise say plainly it isn't done yet."
        )
    pointed = _button_note(words, button=button, held=held)
    if pointed:
        return pointed
    if asks_for_photo(words) and not button and PHOTO_TOOL in names:
        return (
            f"Your words ask them for a photo, but no button for it is under your message. "
            f"Call {PHOTO_TOOL} with their words, and its button shows there."
        )
    if tacks_on(words, said=said):
        return (
            "Not sent yet: after answering what they asked, your words bring up something else "
            "in their books they didn't ask about. Write your reply to them again with only the "
            "answer to what they asked; hold the rest for when they ask what's waiting or what "
            "to do next. If they did ask about it, send your reply as it was. Never mention "
            "this note."
        )
    return ""


__all__ = [
    "PHOTO_TOOL",
    "asks_for_photo",
    "asks_two",
    "card_number",
    "claims_done",
    "denies_hers",
    "denies_the_button",
    "misleads",
    "points_to_a_button",
    "hers_note",
    "problem",
    "says_its_so",
    "tacks_on",
    "tool_named",
]
