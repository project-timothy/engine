"""Which of Tim's calls run and which wait for her own tap (Standalone Tim,
shot 1, card V2, the owner's rule of 2026-11-17).

"A write that carries her own words files directly, as today. A decision,
or an amount Tim inferred, shows a confirm card she taps. Card decisions
always go to the existing yes page."

The wall is this code, never the model's good behavior: a model that obeys
every instruction a receipt or a payee's name carries still cannot change
the books without her tap, because the loop will not run the call. Every
tool is one of four kinds:

- **read**: ``ToolSpec.annotations`` is ``None``, the catalog's own mark for
  read-only (core/tools/catalog.py). It runs.
- **her own words** (:data:`OWN_WORDS`): a short named set of writes that
  file one thing she said. It runs only when every amount in its arguments
  is one she said in this message (:func:`her_own`); any other amount is
  one Tim inferred, and waits for her tap.
- **the yes page** (:data:`YES_PAGE`): deciding a card is never done by
  word from Tim's page. The loop asks for her own yes page (the
  doorkeeper's, Face ID or the emailed link) at every amount.
- **a decision**: every other write. It waits for her tap. That is the
  default, so a tool added later is held until someone names its kind;
  :data:`DECISIONS` lists today's so a test fails on one nobody named.

What the card says is built here from the tool and its arguments
(:func:`summary`), never from the model's words.

Her typed yes (the owner's call of 2026-11-18) is read here too, from her
own message alone: :func:`asks_for` (under the ministry's line, her own
words asking to approve or reject decide her own card, the decider's
by-word rule doing the rest) and :func:`plain_yes` (right after a held
call, a plain yes as her next message is her tap).
"""

from __future__ import annotations

import re
import tomllib
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

READ = "read"
OWN = "own_words"
YES = "yes_page"
DECISION = "decision"

YES_PAGE = "decide_card"
"""The one tool that decides a card: always her own yes page."""

OWN_WORDS = frozenset(
    {
        "add_receipt",  # a receipt she says she has; the amount is read from her words
        "count_cash",  # the cash she says she has in hand
        "explain_cash",  # where she says missing cash went
        "paid_it_myself",  # an expense she says she paid; it raises a card for its yes
        "pick_card_receipt",  # which receipt she says a card line paid
        "set_amount",  # how much she says a receipt was
        "set_category",  # the category she says a receipt goes under
        "set_drive_purpose",  # what she says a drive was for
        "set_receipt_day",  # the day she says a receipt is from
        "split_line",  # the part of a line she says was something else
    }
)
"""Writes that file what she said. Each runs on her word alone when every
amount in it is hers."""

NEEDS_AMOUNT = frozenset({"set_amount", "explain_cash"})
"""Her-words writes that infer an amount when her words carry none (the
photo's reading; all the cash still missing): with no amount of hers, a
card."""

DECISIONS = frozenset(
    {
        "agree_to_readings",
        # What a bank line was for waits for her Yes (the owner, 2026-11-20,
        # board w-talk-bank-line): the card says who, what for, how much, and
        # where it is filed, before the books keep it.
        "explain_bank_line",
        "ask_for_ok",
        "bank_statement",
        "catch_up_link",
        "close_year",
        "draft_rule",
        "keep_own_lines",
        "keep_payers",
        "keep_school_list",
        "keep_tax_facts",
        "keep_travel",
        "letter_details",
        "record_reading",
        "reopen_year",
        "send_month_for_review",
        "set_budget",
        "set_pay",
        "set_program_share",
    }
)
"""Today's decisions, named so a new write tool fails a test until someone
says its kind. A write named nowhere is held all the same."""

REFERENCES = frozenset({"line", "receipt", "day"})
"""Arguments that name a record or a day, never an amount."""

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
WORDS_FILE = Path(__file__).with_name("talk.toml")


def kind(spec: Any) -> str:
    """Which of the four kinds a tool is."""
    if spec.name == YES_PAGE:
        return YES
    if spec.annotations is None:
        return READ
    if spec.name in OWN_WORDS:
        return OWN
    return DECISION


def amounts(text: str) -> set[Decimal]:
    """Every number in ``text``, as a value: "$1,300.00", "1300" and
    "1,300" are one amount."""
    out: set[Decimal] = set()
    for found in _NUMBER.findall(text):
        try:
            out.add(Decimal(found.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def _values(args: dict) -> list[str]:
    """Every argument a number could hide in, but the ones naming a record."""
    out: list[str] = []
    for key, value in args.items():
        if key in REFERENCES or isinstance(value, bool):
            continue
        if isinstance(value, list | dict):
            out += _values(
                value if isinstance(value, dict) else {str(i): v for i, v in enumerate(value)}
            )
        elif value is not None:
            out.append(str(value))
    return out


def her_own(tool: str, args: dict, said: str) -> bool:
    """Whether a her-words call may run on her word: every amount in it is
    one she said in this message, and one that needs an amount has one."""
    hers = amounts(said)
    found: set[Decimal] = set()
    for value in _values(args):
        found |= amounts(value)
    if tool in NEEDS_AMOUNT and not found:
        return False
    return found <= hers


# ---- what the card says ---------------------------------------------------------------

_MONTHS = (
    "January February March April May June July August September October November December"
).split()
MOST = 60
"""Characters of any one argument a card repeats."""


@cache
def lines() -> dict[str, str]:
    """What a card says for each tool, with ``{names}`` code fills."""
    return dict(tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))["card"])


def _short(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= MOST else text[: MOST - 3].rstrip() + "..."


def _month(value: Any) -> str:
    text = str(value or "")
    if re.fullmatch(r"\d{4}-\d{2}", text) and 1 <= int(text[5:]) <= 12:
        return f"{_MONTHS[int(text[5:]) - 1]} {text[:4]}"
    return _short(text)


def _money(value: Any) -> str:
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return _short(value)
    return f"${amount:,.2f}"


def _how_much(args: dict) -> str:
    for key in ("amount", "yearly", "form_says", "work_part", "estimate_paid", "spouse_wages"):
        if args.get(key) not in (None, ""):
            return _money(args[key])
    said = sorted(amounts(" ".join(_values({"said": args.get("said", "")}))))
    return ", ".join(f"{n:,}" for n in said)


def _filed(args: dict) -> str:
    """Where a bank line's purpose files it, from the call's own arguments."""
    whose = str(args.get("whose") or "").strip()
    giver = _short(args.get("gift_from") or "")
    if giver and whose == "own":
        return f"as a gift to you from {giver}, kept off your taxes"
    if giver:
        return f"as a gift to the ministry from {giver}"
    if args.get("pays_back"):
        return f"as paying {_short(args['pays_back'])} back"
    if args.get("pay_of"):
        return f"as {_short(args['pay_of'])}'s pay"
    if whose == "own":
        return "as your own, never the ministry's"
    if whose == "pay":
        return "as a ministry cost paid from your own pay"
    if args.get("category"):
        return f"under {_short(args['category'])}"
    return "as the ministry's"


def summary(tool: str, args: dict, *, you: str) -> str:
    """One plain line for a card: whose, what for, and how much, from the
    tool and its arguments alone. ``you`` is her own name, so a card about
    her reads "your"."""
    person = str(args.get("person") or "").strip()
    whose = "your" if not person or person == you else f"{_short(person)}'s"
    much = _how_much(args)
    fill = {
        "whose": whose,
        "month": _month(args.get("month")),
        "year": _short(args.get("year") or ""),
        "much": f": {much}" if much else "",
        "what": _short(args.get("what") or ""),
        "kind": _short(args.get("kind") or args.get("category") or ""),
        "payer": _short(args.get("payer") or ""),
        "country": _short(args.get("country") or ""),
        "document": _short(args.get("document") or ""),
        "percent": _short(args.get("percent") or ""),
        "person": _short(person),
    }
    card = lines()
    if tool == "explain_bank_line":
        fill |= {"said": _short(args.get("said") or ""), "filed": _filed(args)}
    if tool == "keep_payers" and args.get("form_says") not in (None, ""):
        tool = "keep_payers_form"  # what a tax form says, never who pays (V4 Y02)
    line = card.get(tool, card["any"])
    return " ".join(line.format(**fill).split()).replace(" :", ":")


# ---- her typed yes (the owner's call of 2026-11-18) ------------------------------------

_ASKS = {
    "approve": re.compile(r"\b(?:approv\w*|sign(?:s|ed|ing)?\s+off)\b"),
    "reject": re.compile(r"\b(?:reject\w*|declin\w*|den(?:y|ies|ied))\b"),
}
_NOT = re.compile(
    r"\b(?:don'?t|do\s+not|never|not|no|won'?t|can'?t|cannot|shouldn'?t|without)"
    r"(?:\s+\w+){0,2}\s*$"
)
_ASKING_WHETHER = re.compile(r"\b(?:can|could|may|should|do|how)\s+i\b")


def asks_for(said: str, verb: str) -> bool:
    """Whether her own message asks for this decision ("approve my August
    report", "please sign off on it", "reject the water bill"): the word
    for it, with no "don't" or "never" before it, and not a question about
    whether she may ("can I approve it?"). Naming the card is the
    decider's to check; this is only her asking."""
    pattern = _ASKS.get(verb)
    text = " ".join(said.casefold().replace("\u2019", "'").split())
    if pattern is None or text.endswith("?"):
        return False
    for found in pattern.finditer(text):
        before = text[: found.start()]
        if _NOT.search(before) or _ASKING_WHETHER.search(before):
            continue
        return True
    return False


_NO = re.compile(
    r"^(?:no|nope|nah|not\s+(?:now|yet)|never\s*mind|cancel|stop|hold\s+off)\b"
    r"|\b(?:never\s*mind|not\s+now|hold\s+off|forget\s+(?:it|that|about\s+it)|leave\s+it"
    r"|don'?t\s+(?:approve|reject|decline|do\s+(?:it|that))|cancel\s+(?:it|that))\b"
)


def says_no(said: str, verb: str) -> bool:
    """Whether her message turns down a decision she asked for ("never mind",
    "not now", "leave it", or asking for the other one: "actually reject
    it"), so code does not put its yes button back (``core.talk.loop``).
    "It didn't work" and "the link expired" are no such thing."""
    text = " ".join(said.casefold().replace("\u2019", "'").split())
    other = {"approve": "reject", "reject": "approve"}.get(verb, "")
    return bool(_NO.search(text)) or (bool(other) and asks_for(said, other))


_FIRM = frozenset(
    "yes yeah yep yup ya ok okay sure correct right confirm confirmed absolutely definitely good "
    "fine".split()
)
_ALSO = frozenset(
    "please do it that s thats is right sounds looks go ahead thanks thank you tim great perfect "
    "alright all exactly record keep add save one this".split()
)
PLAIN_MOST = 8
"""Words in a plain yes, at most."""


_DECIDED = {
    "approve": frozenset("approve approved approving approves sign signed signing off".split()),
    "reject": frozenset("reject rejected rejecting decline declined declining deny denied".split()),
}
_DECIDING = frozenset("i on".split())


def plain_decision(said: str) -> str:
    """Her whole message, when it is a plain yes or no to a decision and
    nothing more ("It's approved.", "approve it", "reject it", "ok sign off on
    it"): "approve" or "reject", else "". The T2T exchange of 2026-11-19:
    "It's approved." was taken for news. Every word one such a message is
    made of, at least one of them the decision, and not a question; "my visa
    got approved" and "is it approved?" are words for Tim."""
    text = said.casefold().replace("\u2019", "'").strip()
    tokens = re.findall(r"[a-z]+", text)
    if not tokens or len(tokens) > PLAIN_MOST or text.endswith("?"):
        return ""
    allowed = _FIRM | _ALSO | _DECIDING | _DECIDED["approve"] | _DECIDED["reject"]
    if not set(tokens) <= allowed:
        return ""
    verbs = [verb for verb, words in _DECIDED.items() if set(tokens) & (words - {"off"})]
    return verbs[0] if len(verbs) == 1 else ""


def plain_yes(said: str, args: dict) -> bool:
    """Whether her message is a plain yes and nothing more ("yes", "ok, do
    it", "yep that's right"): every word one a yes is made of, at least one
    of them a yes, and any amount in it one on the held call (``args``).
    Anything else ("yes but make it $300", "not now", "no") is words for
    Tim, never her tap."""
    tokens = re.findall(r"[a-z]+", said.casefold().replace("\u2019", "'"))
    if not tokens or len(tokens) > PLAIN_MOST:
        return False
    if not set(tokens) <= _FIRM | _ALSO or not set(tokens) & _FIRM:
        return False
    held: set[Decimal] = set()
    for value in _values(args):
        held |= amounts(value)
    return amounts(said) <= held
