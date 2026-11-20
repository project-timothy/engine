"""One message in, Tim's reply out (Standalone Tim, shot 1, card V1).

:func:`reply` holds one turn of the conversation on the box. The model
never touches the books: each model call returns exactly one of a tool to
call or the words to say (:class:`Turn`, the runner's one-turn-per-call
pattern, ``core/llm/runner.py``), and this loop runs the tool in-process
through the same ``Tools.call`` every door uses, as the signed-in person.
What the person may see, what a tool may write, and every figure are the
tools' own (invariant 2); a tool the person does not have cannot be called.

The call goes through the tenant's model policy (``core.llm.policy``), job
type ``talk``: the ministry's own file names the model, every call is
priced and recorded, and the monthly cap refuses a call before it is made.
A ministry whose file names no tier for ``talk`` simply has no voice yet.
A hard question (taxes, a bank statement, setup, the same question again)
steps up to ``talk_hard`` by rule, in code (card V3, :mod:`core.talk.speeds`);
past the cap's soft line it never steps up, and Tim says once a day that he
is near his month's limit.
When the model cannot answer (no voice, the cap, a refusal, a timeout),
Tim says so in plain words from ``talk.toml``; nothing raises to the page.

What a tool returns goes back to the model fenced as data, with every ``<``
escaped so nothing inside can close the fence: a receipt, a payee's name,
or a bank line is read, never obeyed (the brief says the same).

A yes is the person's own tap (card V2, :mod:`core.talk.hold`). A read
runs; a write that files her own words runs when every amount in it is
hers; any other write, or an amount Tim inferred, is held with its exact
arguments and shown to her as a Yes button, and her tap runs exactly that
(:func:`tapped`). A held call no longer ends his turn: the model hears it
waits, in the card's own words, and Tim answers her in his, with the Yes
under them (one card a message, and "it's done" goes back while it waits).
On her next message Tim hears, from code, what her tap on
the button under his last reply did (:func:`_since`): a photo came in, her
yes went through, or the button went unused.
Deciding a card always goes to her own yes page, the one
the month page's Approve button opens, at every amount. The model never
holds a yes, whatever a receipt tells it.

Her typed yes counts too (the owner's call of 2026-11-18), read by code from
her own message, never from the model's: under the ministry's line her own
words asking to approve (or reject) decide her own card by the decider's
by-word rule, the connector door's (:func:`core.talk.hold.asks_for`; over
the line, or several cards at once, her yes page as before), and a plain
yes as her next message after a held call runs it exactly as her tap would
(:func:`core.talk.hold.plain_yes`).
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, model_validator

from ..engine.config import load_tenant
from ..llm.gateway import Adapter, GatewayTransportError, GatewayValidationError, Message
from ..llm.policy import BudgetExceeded, LlmPolicyError, complete_metered
from ..tools.viewer import display_name
from . import check, hold, speeds
from .meter import Meter
from .store import Conversation, Waiting

JOB = speeds.EVERYDAY
"""The job type a tenant's ``[llm.jobs]`` routes to switch Tim's voice on;
``speeds.HARD`` is the one a hard question steps up to (card V3)."""

MAX_STEPS = 8
"""Model calls one message may take before Tim stops and says so."""

HISTORY = 10
"""Past exchanges read back to the model."""

SITTING = timedelta(hours=2)
"""Quiet this long and her next message starts a new sitting: the history
Tim is handed starts clean (the owner, 2026-11-20). Her page still shows the
whole conversation, and what waits on her is code's (``next``, the Yes card),
so nothing she needs is lost."""

RESULT_CAP = 24_000
"""Characters of one tool answer the model is shown."""

BRIEF_FILE = Path(__file__).with_name("brief.md")
WORDS_FILE = Path(__file__).with_name("talk.toml")

MANUAL = (
    "Reply with one JSON object per turn: exactly one of `call` (one tool call) or `say` "
    "(your words to the person, which ends your turn). In `call`, `tool` is the tool's name "
    "and `args_json` is its arguments as a JSON object written out as a string, such as "
    '"{\\"month\\": \\"2026-10\\"}", or "{}" for none. After a call you are shown what it '
    "returned and reply again."
)


class Call(BaseModel):
    tool: str
    args_json: str = "{}"


class Turn(BaseModel):
    """One model turn: one tool call, or the words that end the turn."""

    call: Call | None = None
    say: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Turn:
        if (self.call is None) == (self.say is None):
            raise ValueError("a turn carries exactly one of call or say")
        return self


@dataclass(frozen=True)
class Reply:
    """What Tim says back. ``status`` is ``ok`` or why the model could not
        answer (``off``, ``capped``, ``declined``, ``trouble``); ``calls`` names
        the tools that ran; ``next`` is the one thing waiting on the person as
        the last tool answer gave it (code's, never the model's); ``waiting`` is
    what now waits on her own tap: a held call or her yes page (code's);
    ``near``: Tim said he is near his month's limit (card V3)."""

    say: str
    status: str = "ok"
    calls: list[str] = field(default_factory=list)
    next: dict[str, Any] | None = None
    waiting: dict[str, Any] | None = None
    near: bool = False
    made: int | None = None
    """The card a write this message left waiting on her (code's)."""


@cache
def words() -> dict[str, str]:
    """Tim's own lines for when the model cannot answer."""
    return dict(tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))["words"])


@cache
def _brief() -> str:
    return BRIEF_FILE.read_text(encoding="utf-8").strip()


def _system(tools: Any, offered: dict[str, Any]) -> str:
    """The brief, the person's own tools, and who is speaking. The same
    text on every turn of a person's day, so the provider can cache it.
    Each tool in its short form when it has one (card C1): the list goes
    out on every turn, and the door's longer words stay the door's."""
    lines = [_brief(), "", "## Your tools", "", MANUAL, ""]
    for name in sorted(offered):
        spec = offered[name]
        lines.append(f"- {name}: {spec.short or spec.description}")
        if spec.schema.get("properties"):
            takes = json.dumps(spec.schema, sort_keys=True, separators=(",", ":"))
            lines.append(f"  arguments: {takes}")
    who = display_name(tools.viewer.person) if tools.viewer is not None else "the owner"
    of = f" of {tools.ministry}" if tools.ministry else ""
    today = tools.today or date.today().isoformat()
    lines += ["", f"You are speaking with {who}{of}. Today is {today}."]
    return "\n".join(lines)


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|/)[^)\s]*\)")
_SHOW_AS_MARKDOWN = re.compile(r"Show (?:the person )?the show line as it is: a Markdown link\.\s*")
ON_HER_PAGE = (
    "Their page shows this as a button under your message. Tell them to tap it there; "
    "never write the link or the show line. "
)
"""What a link-making tool's answer tells the model on Tim's own page (the
persona year on his voice, 2026-11-18: the receipt link reached the page as
raw Markdown, with nothing to tap)."""


HELD = (
    "Not run: it waits for their tap on the Yes button under your message, which reads "
    '"{line}". '
    "Don't call it again this message, and never say it's done. In your words, "
    "say in a sentence what that Yes would keep and why, then answer anything else they asked."
)
"""What the model hears when a call waits for her tap (the persona year on his
voice, run 3, 2026-11-18: a held call ended Tim's turn on the stock line, so
her question went unanswered and the same card came back three times)."""
HELD_ALREADY = (
    'Not run: a button already waits under your message ("{line}"), and only one waits at '
    "a time. Answer them in words; this can be asked again after they tap it."
)
LINK_HELD_BACK = (
    'Not put up: their Yes ("{line}") already waits under your message, and only one button '
    "waits at a time. Ask for this after they tap it."
)
YES_BACK = (
    "Their yes under this reply hasn't gone through, and the card still waits on them, so its "
    "yes button is back under your reply now. Don't call decide_card for it again this "
    "message. In a sentence, say the button is there for them to tap, then answer anything "
    "else they asked."
)
"""What the model hears when code put her yes button back (:func:`_yes_again`)."""
YES_UP = (
    "Not asked again: the yes button for that card is already right below the message you're "
    "writing now. In your words, say it's right below this message for them to tap, then "
    "answer anything else they asked."
)
YES_FIRST = (
    "Their words asked to {verb} the card your last reply left waiting on them, so its yes "
    "button is right below the message you're writing now. Don't call decide_card for it "
    "again this message. In a sentence, say their yes button is right below this message, for "
    "them to tap with Face ID or a fingerprint, then answer anything else they asked."
)
"""What the model hears when code asked for her yes page on her first yes
(:func:`_first_yes`)."""
YES_AGAIN_WITHIN = timedelta(minutes=30)
"""How long after she asked for a decision code puts her yes button back, once, when it
didn't decide the card (the owner's call of 2026-11-18, the persona year's A01)."""


@dataclass
class _Up:
    """What one message has put up for her so far: the one thing waiting
    (``nxt``), a link she taps (``button``), and a held call's Yes card
    (``card``), which stands whatever the model does after it."""

    nxt: dict[str, Any] | None = None
    button: dict[str, Any] | None = None
    card: dict[str, Any] | None = None
    done: list[str] = field(default_factory=list)
    """Each write it finished, as its Yes button would say it, for a turn
    that breaks partway to name (the owner's read of 2026-11-19)."""
    unsure: bool = False
    """A write ran whose answer doesn't say plainly whether it changed her
    books, so a broken turn can't say nothing else changed."""
    made: int | None = None
    """The card a write this message left waiting on her, as its answer
    gave it, for her next words to decide (:func:`_first_yes`)."""


DONE = frozenset(
    {
        "set",
        "kept",
        "placed",
        "agreed",
        "approved",
        "rejected",
        "taken_back",
        "voted",
        "settled",
        "closed",
        "sent",
        "asked",
        "drafted",
    }
)
"""A write's answer status that says it changed her books."""

NOT_DONE = frozenset(
    {
        "needs",
        "needs_category",
        "needs_currency",
        "needs_amount",
        "refused",
        "asks",
        "busy",
        "not_found",
        "which",
        "pick",
        "year_closed",
        "not_a_day",
        "not_a_month",
        "not_yet",
        "not_needed",
        "nothing_to_tap",
        "empty",
        "already",
        "already_kept",
        "tap_to_confirm",
        "tap_to_add",
        "waiting",
        "emailed",
    }
)
"""A write's answer status that says it changed nothing."""


def _said(say: str | None) -> str:
    """The model's words as she reads them. Her page shows his words as
    plain text, so a Markdown link keeps only its words (the link itself
    shows as a button). A closing brace at the end with no opening one is
    its reply's own JSON landing inside its words (the second live trial,
    2026-11-18: "page 2.}"), and is trimmed."""
    text = _MARKDOWN_LINK.sub(r"\1", (say or "").strip())
    while text.endswith("}") and text.count("}") > text.count("{"):
        text = text[:-1].rstrip()
    return text


def _page_link(answer: Any) -> tuple[str, str] | None:
    """(the button's words, the link) when a tool's answer is a link for her
    to tap (``show`` as one Markdown link); None otherwise."""
    if not isinstance(answer, dict) or answer.get("status") != "tap_to_add":
        return None
    found = re.fullmatch(r"\[([^\]]+)\]\(([^)\s]+)\)", str(answer.get("show") or "").strip())
    return (found.group(1), found.group(2)) if found else None


def _for_her_page(answer: dict) -> dict:
    """A link-making tool's answer as the model sees it on her page: no link
    to copy, and the note that the page shows the button."""
    out = {k: v for k, v in answer.items() if k != "show"}
    out["say"] = ON_HER_PAGE + _SHOW_AS_MARKDOWN.sub("", str(answer.get("say") or ""))
    return out


def _note(text: str) -> str:
    return f"<note>{text}</note>"


def _fenced(tool: str, answer: Any) -> str:
    """A tool's answer as data: JSON between the fence, every ``<`` escaped
    so no text inside can close it."""
    body = json.dumps(answer, default=str).replace("<", "\\u003c")
    if len(body) > RESULT_CAP:
        body = body[:RESULT_CAP] + " ... (cut short: ask for less at a time)"
    return f'<books tool="{tool}">\n{body}\n</books>'


def _args(offered: dict[str, Any], call: Call) -> tuple[dict | None, str]:
    """(the call's arguments, "") or (None, the note saying why nothing ran)."""
    if call.tool not in offered:
        names = ", ".join(sorted(offered))
        return None, _note(f"{call.tool} is not one of your tools. Your tools: {names}.")
    try:
        args = json.loads(call.args_json or "{}")
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        return None, _note("args_json must be a JSON object written as a string. Nothing ran.")
    return args, ""


def _run(tools: Any, tool: str, args: dict) -> tuple[str, Any]:
    """Run one call as the person: (what the model is shown, the answer or
    ``None`` when it did not work)."""
    try:
        answer = tools.call(tool, args)
    except Exception as exc:  # noqa: BLE001  a tool's failure is the model's to put in plain words
        return _note(f"{tool} did not work: {type(exc).__name__}: {exc}"), None
    return _fenced(tool, answer), answer


def _run_tapped(tools: Any, tool: str, args: dict) -> tuple[str, Any]:
    """Her tap's call, run as her (:func:`_run`). A take-back held for her
    tap goes to the decider as her tap, the one way past its words: no model
    can reach this path (:meth:`_Turns._take_back_near`)."""
    from ..tools.decide import TAKE_BACK

    if tool != hold.YES_PAGE or args.get("decision") != TAKE_BACK:
        return _run(tools, tool, args)
    try:
        answer = tools.decider.decide(tools, args, tap=True)
    except Exception as exc:  # noqa: BLE001  a refusal is the model's to put in plain words
        return _note(f"{tool} did not work: {type(exc).__name__}: {exc}"), None
    return _fenced(tool, answer), answer


def sitting(entries: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """The exchanges of her current sitting: back from now until a quiet
    gap of :data:`SITTING` or more."""
    out: list[dict[str, Any]] = []
    later = now
    for entry in reversed(entries):
        try:
            at = datetime.fromisoformat(str(entry.get("at", "")))
        except ValueError:
            break
        if later - at >= SITTING:
            break
        out.append(entry)
        later = at
    return out[::-1]


def _history(
    entries: list[dict[str, Any]],
    offered: dict[str, Any],
    since: str = "",
    fresh: list[dict[str, Any]] | None = None,
) -> list[Message]:
    """Past exchanges as the model sees them: her words as she typed them,
    then Tim's reply with ``read_from``, the tools whose answers it was read
    from (issue #681: without them the model called its own true figures
    made up), and ``wrote``, the ones that wrote to her books, which stand
    (the persona year on his voice, 2026-11-18: with a write listed as a
    read, Haiku took back a gift it had recorded). What those tools returned
    then is not kept; the brief says to call them again before repeating an
    amount. A tool no longer offered is listed as read. ``since``: what her
    tap on the button under the last reply did (:func:`_since`), kept on it.
    ``fresh``: the reads behind the last reply, run again just now
    (:meth:`_Turns._fresh`), kept on it as ``books_now``."""
    out: list[Message] = []
    for entry in entries:
        past: dict[str, Any] = {"say": entry["tim"]}
        ran = [str(tool) for tool in entry.get("tools") or []]
        wrote = [tool for tool in ran if tool in offered and hold.kind(offered[tool]) != hold.READ]
        read = [tool for tool in ran if tool not in wrote]
        if read:
            past["read_from"] = read
        if wrote:
            past["wrote"] = wrote
        if since and entry is entries[-1]:
            past["since"] = since
        if fresh and entry is entries[-1]:
            past["books_now"] = fresh
        out += [Message("user", str(entry["said"])), Message("assistant", json.dumps(past))]
    return out


def _since(tools: Any, shown: dict[str, Any] | None) -> str:
    """What her tap on the button under Tim's last reply did, from code, for
    his next answer to hear (the persona year on his voice, run 3,
    2026-11-18: S13, the photo was in and Tim asked for it again; A01, her
    yes went through and Tim said "once you finish the Face ID step"). ""
    when there was no such button, or nothing to say about it."""
    if not shown:
        return ""
    kind = shown.get("kind")
    if kind == "link":
        return _link_since(tools, str(shown.get("link") or ""))
    if kind == "yes_page" and shown.get("cards"):
        return _yes_since(tools, [str(c) for c in shown["cards"]])
    return ""


def _link_since(tools: Any, link: str) -> str:
    desk = getattr(getattr(tools, "receipts", None), "desk", None)
    token = link.split("#", 1)[1] if "#" in link else ""
    if desk is None or not token or desk.is_pile(token):
        return ""  # a catch-up link takes many sends; its page says how they went
    gone = desk.gone(token)
    if gone == "unknown":
        return ""  # the box restarted since, and its receipt desk forgot: say nothing
    if gone == "used":
        return (
            "Since this reply, their tap on the receipt button under it worked: the photo came in."
        )
    return (
        "The receipt button under this reply went unused and is gone from their page now; "
        "add_receipt makes a new one when they have the receipt."
    )


def _stands(tools: Any, cards: list[str]) -> list[str]:
    """How the cards under a yes page stand now, each status once."""
    ids = [int(c) for c in cards if c.isdigit()]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = tools._rows(
        f"SELECT status FROM approval_queue WHERE tenant = ? AND id IN ({marks})",  # noqa: S608
        (tools.tenant, *ids),
    )
    return sorted({str(r["status"]) for r in rows})


def _yes_since(tools: Any, cards: list[str]) -> str:
    stands = _stands(tools, cards)
    if not stands:
        return ""
    if "pending" in stands:
        return (
            "Their yes page under this reply hasn't been tapped yet, and its button is gone "
            "from their page; decide_card brings it back if they still want to say yes."
        )
    if set(stands) <= {"approved", "rejected"}:
        decided = " and ".join(stands)
        return f"Since this reply, their tap on the yes page under it decided it: {decided}."
    return "Since this reply, what their yes page was for no longer waits on them."


def _yes_again(
    tools: Any, shown: dict[str, Any] | None, said: str, waiting: Waiting, instant: datetime
) -> dict[str, Any] | None:
    """Her yes button back under Tim's next reply, put up by code (the owner's
    call of 2026-11-18; the persona year's A01: her yes page ran out on her
    tap, and Tim said a fresh one was the right step without asking for it,
    whatever his note said). Only for a yes page her own words asked for
    (never one a receipt or a bank line talked the model into), once,
    within 30 minutes of her ask, when it didn't decide its card and her
    new message doesn't turn it down. The
    same request as before, to her own yes page: nothing is decided until
    her Face ID on it. The button her page shows, or None."""
    if not shown or shown.get("kind") != "yes_page" or shown.get("again"):
        return None
    ask, cards = shown.get("ask"), [str(c) for c in shown.get("cards") or []]
    decider = getattr(tools, "decider", None)
    if not isinstance(ask, dict) or not cards or decider is None:
        return None
    try:
        asked = datetime.fromisoformat(str(shown.get("asked")))
    except ValueError:
        return None
    if instant - asked > YES_AGAIN_WITHIN or hold.says_no(said, str(ask.get("decision", ""))):
        return None
    if _stands(tools, cards) != ["pending"]:
        return None
    try:
        out = decider.decide(tools, dict(ask), tap=True)
    except Exception:  # noqa: BLE001  no button, and Tim hears what it showed before
        return None
    if not isinstance(out, dict) or out.get("vouched_by") or not out.get("link"):
        return None
    return waiting.yes_page(
        str(out["link"]), now=instant, card=cards, ask=ask, asked=str(shown["asked"])
    )


def _person(tools: Any) -> str:
    return tools.viewer.person if tools.viewer is not None else "owner"


def reply(
    tools: Any,
    said: str,
    *,
    tenant: str,
    ledger_root: str | Path,
    tenants_root: str | Path | None = None,
    adapter: Adapter | None = None,
    max_steps: int = MAX_STEPS,
    now: datetime | None = None,
    in_setup: Callable[[], bool] | None = None,
) -> Reply:
    """Tim's reply to what the person said, with their tools (a
    ``core.tools.catalog.Tools`` already bound to them). ``adapter`` is for
    tests: a scripted model in place of the tier's own. ``in_setup``: whether
    she still has a setup step ahead (the page's; a hard-question rule). The
    exchange is added to their conversation beside the books."""
    instant = now or datetime.now(UTC)
    waiting = Waiting(ledger_root, tenant, _person(tools))
    held = waiting.shown(now=instant)
    if held and held.get("kind") == "confirm" and hold.plain_yes(said, dict(held["args"])):
        # A plain yes right after his card is her tap on its Yes, in her own words.
        typed = tapped(
            tools,
            str(held["id"]),
            yes=True,
            tenant=tenant,
            ledger_root=ledger_root,
            tenants_root=tenants_root,
            adapter=adapter,
            max_steps=max_steps,
            now=instant,
            in_setup=in_setup,
            said=said,
        )
        if typed is not None:
            return typed
    talk = Conversation(ledger_root, tenant, _person(tools))
    last = waiting.last()
    since = _since(tools, last)  # what her tap on his last button did
    waiting.drop()  # she moved on: an older Yes goes
    again = _yes_again(tools, last, said, waiting, instant)  # or her yes, back once
    turns = _Turns(tools, talk, tenant, ledger_root, tenants_root, adapter, instant)
    first = None if again is not None else turns.first_yes(said)  # or her first yes
    if isinstance(first, Reply):  # her yes page went by email: code's words end it
        talk.add(said, first.say, first.calls, now=instant, status=first.status)
        return first
    speed = turns.speed(said, in_setup)
    if first is not None and first[0] == "ran":  # under the line: her word decided it
        speed.called(hold.YES_PAGE)
        answer = turns.answer(
            said, max_steps, ran=first[1], calls=[hold.YES_PAGE], speed=speed, since=since
        )
    else:
        button = again if again is not None else (first[1] if first is not None else None)
        note = YES_BACK if again is not None else (first[2] if first is not None else since)
        answer = turns.answer(said, max_steps, speed=speed, since=note, button=button)
    talk.add(
        said,
        answer.say,
        answer.calls,
        now=instant,
        status=answer.status,
        near=answer.near,
        made=answer.made,
        reads=turns.reads,
    )
    return answer


def tapped(
    tools: Any,
    given: str,
    *,
    yes: bool,
    tenant: str,
    ledger_root: str | Path,
    tenants_root: str | Path | None = None,
    adapter: Adapter | None = None,
    max_steps: int = MAX_STEPS,
    now: datetime | None = None,
    in_setup: Callable[[], bool] | None = None,
    said: str | None = None,
) -> Reply | None:
    """Her tap on the Yes or Not now under Tim's message (card V2). On Yes,
    code runs exactly the held call, as her, once; then Tim says how it
    went. None when there is no such call waiting on her (an old button, or
    one past its 15 minutes). ``said``: the plain yes she typed in place of
    the tap (:func:`reply`), kept as her words."""
    instant = now or datetime.now(UTC)
    person = _person(tools)
    entry = Waiting(ledger_root, tenant, person).take(given, now=instant)
    if entry is None:
        return None
    talk = Conversation(ledger_root, tenant, person)
    lines = words()
    named = str(entry.get("summary") or "")
    if not yes:
        answer = Reply(lines["not_now"])
        tap = {"answer": "no", "what": named} if named else None
        talk.add(lines["tapped_not_now"], answer.say, [], now=instant, tap=tap)
        return answer
    heard = said or lines["tapped_yes"]
    tool, args = str(entry["tool"]), dict(entry["args"])
    turns = _Turns(tools, talk, tenant, ledger_root, tenants_root, adapter, instant)
    offered = turns.offered
    if tool in offered:
        shown, ran = _run_tapped(tools, tool, args)
    else:  # no longer hers to do (her role changed in the 15 minutes)
        shown, ran = _note(f"{tool} is not one of your tools any more. Nothing ran."), None
    calls = [tool] if ran is not None else []
    speed = turns.speed(None, in_setup)  # a tap carries no words of hers
    speed.called(tool)
    answer = turns.answer(
        heard,
        max_steps,
        ran=(Call(tool=tool, args_json=json.dumps(args)), shown),
        calls=calls,
        fallback=lines["tapped_ran" if ran is not None else "tapped_failed"],
        speed=speed,
    )
    talk.add(
        heard,
        answer.say,
        answer.calls,
        now=instant,
        status=answer.status,
        near=answer.near,
        # her button, never her typed yes: those are her own words
        tap={"answer": "yes", "what": named} if named and said is None else None,
    )
    return answer


class _Turns:
    """One message's model turns, as the person, under the four kinds."""

    def __init__(
        self,
        tools: Any,
        talk: Conversation,
        tenant: str,
        ledger_root: str | Path,
        tenants_root: str | Path | None,
        adapter: Adapter | None,
        instant: datetime,
    ) -> None:
        self.tools, self.talk, self.tenant = tools, talk, tenant
        self.ledger_root, self.adapter, self.instant = ledger_root, adapter, instant
        root = Path(tenants_root) if tenants_root else None
        self.settings = load_tenant(tenant, tenants_root=root).llm
        self.meter = Meter(ledger_root)
        self.offered = {spec.name: spec for spec in tools.specs()}
        self.waiting = Waiting(ledger_root, tenant, _person(tools))
        self.since = ""
        self.reads: list[dict[str, Any]] = []
        """Each read this message ran: the tool and its arguments, kept with the reply."""

    def _fresh(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """The reads behind his last reply, run again now as her: reads
        only, never a write; one that fails is skipped; all of them together
        held to :data:`RESULT_CAP`."""
        out: list[dict[str, Any]] = []
        room = RESULT_CAP
        for read in entry.get("reads") or []:
            tool, args = str(read.get("tool", "")), read.get("args")
            if tool not in self.offered or hold.kind(self.offered[tool]) != hold.READ:
                continue
            if not isinstance(args, dict):
                continue
            try:
                answer = self.tools.call(tool, dict(args))
            except Exception:  # noqa: BLE001, S112  a read that fails now is simply not shown
                continue
            now = _for_her_page(answer) if _page_link(answer) is not None else answer
            size = len(json.dumps(now, default=str))
            if size > room:
                break
            room -= size
            out.append({"tool": tool, "args": args, "now": now})
        return out

    def speed(self, said: str | None, in_setup: Callable[[], bool] | None) -> speeds.Speed:
        """The speed this message starts at, by rule (card V3)."""
        return speeds.pick(
            self.settings,
            self.meter.month_to_date_usd(self.tenant),  # the clock the cap itself reads
            said=said,
            history=self.talk.recent(now=self.instant),
            today=speeds.at(self.instant),
            in_setup=in_setup,
        )

    def _hold(self, tool: str, args: dict, up: _Up, line: str = "") -> str:
        """Not run: held for her tap, with the line code writes for it
        (``line``, or the tool's own from :func:`hold.summary`), and the note
        that tells the model so, for Tim to answer her in his own words with
        the Yes under them. One card waits at a time."""
        first = up.card or up.button
        if first is not None:
            held = first.get("summary") or first.get("text") or words()["yes_page"]
            return _note(HELD_ALREADY.format(line=held))
        if not line:
            you = self.tools.viewer.person if self.tools.viewer is not None else ""
            line = hold.summary(tool, args, you=display_name(you) if you else "")
        up.card = self.waiting.hold(tool, args, line, now=self.instant)
        return _note(HELD.format(line=line))

    def first_yes(self, said: str) -> Reply | tuple[Any, ...] | None:
        """Her words deciding the card Tim's last reply left waiting on her,
        asked for by code as the model would have (the owner's call of
        2026-11-19; the Haiku year's A01: Eli typed "yes, go ahead and
        approve it", Tim never asked for the yes page, and told him to tap a
        button that wasn't there). Only when her words ask for one decision
        (``hold.asks_for``: never a question about whether she may, never a
        "don't"), within 30 minutes of that reply, while the card still waits.
        Over the line: ``("button", page, note)``, her yes page under the
        reply, decided by nothing but her Face ID on it. Under it, where her
        word decides by the decider's own rule: ``("ran", (call, shown))``.
        A yes page that went by email: the Reply in code's words. Otherwise
        None, and the model answers her as before."""
        entries = self.talk.recent(now=self.instant)
        made = entries[-1].get("made") if entries else None
        verbs = [verb for verb in ("approve", "reject") if hold.asks_for(said, verb)]
        if not isinstance(made, int) or len(verbs) != 1 or self.tools.decider is None:
            return None
        try:
            at = datetime.fromisoformat(str(entries[-1].get("at")))
        except ValueError:
            return None
        if self.instant - at > YES_AGAIN_WITHIN or _stands(self.tools, [str(made)]) != ["pending"]:
            return None
        args = {"card": made, "decision": verbs[0]}
        ended = self._yes_page(args, [], None, said)
        if isinstance(ended, Reply):
            if ended.waiting is None:
                return ended  # emailed: code's words, as the model's call would end
            return "button", ended.waiting, YES_FIRST.format(verb=verbs[0])
        shown, answer = ended
        if answer is None:
            return None  # refused or someone else's to confirm: the model answers her
        return "ran", (Call(tool=hold.YES_PAGE, args_json=json.dumps(args)), shown)

    def _take_back_near(self, args: dict, said: str, up: _Up) -> str | None:
        """A take-back her words come close to without asking for it ("oops,
        I didn't mean to approve that"; the owner's read of 2026-11-19):
        held for her tap as a Take back button, its line in code's words
        (whose it is, what for, how much), with her message kept as what she
        said. Words that ask for it go by her word; words that don't come
        close go on to be refused (:meth:`_yes_page`): None for both. A card
        her tap couldn't take back gets no button, only the reason."""
        from ..tools.decide import TAKE_BACK, asks_to_take_back, comes_close_to_take_back

        if str(args.get("decision", "")) != TAKE_BACK or args.get("cards") is not None:
            return None
        if asks_to_take_back(said) or not comes_close_to_take_back(said):
            return None
        try:
            card = int(str(args.get("card", "")).lstrip("#"))
        except ValueError:
            return None
        offer, why = self.tools.decider.take_back_offer(card)
        if offer is None:
            return _note(f"{hold.YES_PAGE} did not work: ValueError: {why}")
        line = hold.lines()["take_back"].format(**offer)
        held = {"card": card, "decision": TAKE_BACK, "said": said}  # her words, never the model's
        return self._hold(hold.YES_PAGE, held, up, line=line)

    def answer(
        self,
        said: str,
        max_steps: int,
        *,
        ran: tuple[Call, str] | None = None,
        calls: list[str] | None = None,
        fallback: str = "",
        speed: speeds.Speed | None = None,
        since: str = "",
        button: dict[str, Any] | None = None,
    ) -> Reply:
        """``said``: her words this message, the only amounts that are hers.
        ``ran``: a call her tap already ran, and what it returned, for Tim to
        put in words; ``fallback`` is what he says if the model can't.
        ``speed``: which model each call runs on (card V3). Past the soft
        line, an answer Tim gives ends with his near-the-limit line, once a
        day. ``button``: her yes button, already back under this reply
        (:func:`_yes_again`)."""
        speed = speed or speeds.Speed()
        self.since = since  # what her tap on his last button did (:func:`_since`)
        out = self._answer(said, max_steps, ran, list(calls or []), fallback, speed, button)
        if out.status == "ok" and speed.tell:
            return replace(out, say=f"{out.say}\n\n{words()['near_cap']}", near=True)
        return out

    def _answer(
        self,
        said: str,
        max_steps: int,
        ran: tuple[Call, str] | None,
        calls: list[str],
        fallback: str,
        speed: speeds.Speed,
        button: dict[str, Any] | None = None,
    ) -> Reply:
        tools = self.tools
        messages = [Message("system", _system(tools, self.offered))]
        past = sitting(self.talk.recent(now=self.instant), self.instant)[-HISTORY:]
        fresh = self._fresh(past[-1]) if past else []
        messages += _history(past, self.offered, self.since, fresh)
        messages.append(Message("user", said))
        if ran is not None:
            messages.append(
                Message("assistant", Turn(call=ran[0]).model_dump_json(exclude_none=True))
            )
            messages.append(Message("user", ran[1]))
        run_key = f"talk:{_person(tools)}:{self.instant.astimezone(UTC).date().isoformat()}"
        up = _Up(button=button)

        def plain(status: str) -> Reply:
            if up.card is not None:  # her Yes stands, in code's words, whatever came after
                return Reply(words()["confirm"], "ok", calls, up.nxt, waiting=up.card)
            back = button if up.button is button else None  # her yes button, back, stands
            if fallback:
                return Reply(fallback, status, calls, up.nxt, waiting=back)
            if status == "trouble" and (up.unsure or up.done):
                if up.unsure:
                    return Reply(words()["trouble_after"], status, calls, up.nxt, waiting=back)
                said = words()["trouble_done"].format(done="; ".join(up.done))
                return Reply(said, status, calls, up.nxt, waiting=back)
            return Reply(words()[status], status, calls, up.nxt, waiting=back)

        if not speeds.voice_on(self.settings):  # no tier, or one that can't hold her talk
            return plain("off")
        checked = 0  # his words go back for another try once a message (core.talk.check),
        # and once more for a card's number or a button that isn't there
        for step in range(max_steps):
            turn = self._complete(speed, messages, run_key)
            if isinstance(turn, str):
                return plain(turn)
            if turn.call is None:  # the validator's other half: the words that end the turn
                said_back = _said(turn.say)
                fix = self._check(
                    said_back, calls, up, skip=checked, last=step == max_steps - 1, said=said
                )
                if not fix:
                    waits = up.card or up.button
                    return Reply(said_back, "ok", calls, up.nxt, waiting=waits, made=up.made)
                checked += 1
                messages.append(Message("assistant", turn.model_dump_json(exclude_none=True)))
                messages.append(Message("user", _note(fix)))
                continue
            shown = self._call(turn.call, said, calls, up, speed)
            if isinstance(shown, Reply):
                return shown
            messages.append(Message("assistant", turn.model_dump_json(exclude_none=True)))
            messages.append(Message("user", shown))
        return plain("trouble")

    def _complete(self, speed: speeds.Speed, messages: list[Message], run_key: str) -> Turn | str:
        """The model's next turn, or why there is none (``capped``, ``off``,
        ``declined``, ``trouble``). A call that gets no answer, or one that
        isn't a turn, goes once to the other model before Tim says so
        (:meth:`core.talk.speeds.Speed.other`); a refusal never does, since
        the other model would be asked the same thing."""
        while True:
            try:
                return complete_metered(
                    self.settings,
                    self.meter,
                    speed.job,
                    messages,
                    Turn,
                    tenant=self.tenant,
                    run_key=run_key,
                    adapter=self.adapter,
                    redact_detail=True,  # a failed reply may quote the person's own words
                    cache=True,
                ).output
            except BudgetExceeded:
                return "capped"
            except LlmPolicyError:
                return "off"
            except GatewayTransportError as exc:
                if exc.cause == "refusal":
                    return "declined"
            except GatewayValidationError:
                pass
            if not speed.other():
                return "trouble"

    def _call(
        self, call: Call, said: str, calls: list[str], up: _Up, speed: speeds.Speed
    ) -> Reply | str:
        """One tool call of the model's: what it is shown back, or the Reply
        that ends the turn (her yes page). A read runs; a write of her own
        words runs; any other write waits for her tap (:meth:`_hold`)."""
        args, shown = _args(self.offered, call)
        if args is None:
            return shown
        tool = call.tool
        speed.called(tool)  # a tax or statement tool: the rest of the turn steps up
        held = hold.kind(self.offered[tool])
        answer: Any = None
        if held == hold.YES:
            back = up.button if up.button is not None and up.button.get("ask") else None
            if back is not None and str(args.get("card", "")) in back.get("cards", []):
                return _note(YES_UP)  # her yes button is already back under this reply
            near = self._take_back_near(args, said, up)
            if near is not None:
                return near
            ended = self._yes_page(args, calls, up.nxt, said)
            if isinstance(ended, Reply):
                return ended
            shown, answer = ended
        elif held == hold.DECISION or (held == hold.OWN and not hold.her_own(tool, args, said)):
            return self._hold(tool, args, up)
        else:
            shown, answer = _run(self.tools, tool, args)
            if answer is not None and held == hold.READ:
                self.reads.append({"tool": tool, "args": args})
        if answer is not None:
            calls.append(tool)
            if held != hold.READ:
                self._wrote(tool, args, answer, up)
            shown = self._took(tool, answer, up, shown)
        return shown

    def _wrote(self, tool: str, args: dict, answer: Any, up: _Up) -> None:
        """A write that ran, kept for a turn that breaks partway: named in
        its Yes button's words when its answer says it changed her books,
        dropped when it says it changed nothing, and otherwise unsure."""
        status = answer.get("status") if isinstance(answer, dict) else None
        if status in NOT_DONE:
            return
        if status in DONE and tool != hold.YES_PAGE:  # a decision's own words need its card
            you = self.tools.viewer.person if self.tools.viewer is not None else ""
            up.done.append(hold.summary(tool, args, you=display_name(you) if you else ""))
        else:
            up.unsure = True

    def _took(self, tool: str, answer: Any, up: _Up, shown: str) -> str:
        """After a call ran: the one thing waiting, as its answer gave it,
        and a link it made for her, kept as a button under his message and
        left out of what the model is shown; returns what it is shown. A
        held Yes already under his message stands, and the link waits."""
        if isinstance(answer, dict) and isinstance(answer.get("next"), dict):
            up.nxt = answer["next"]
        if (
            isinstance(answer, dict)
            and answer.get("status") == "waiting"
            and isinstance(answer.get("card"), int)
        ):
            up.made = answer["card"]  # left waiting on her: her next words may decide it
        link = _page_link(answer)
        if link is not None and up.card is not None:
            return _note(LINK_HELD_BACK.format(line=up.card["summary"]))
        if link is not None and up.button is not None and up.button.get("ask"):
            return _note(LINK_HELD_BACK.format(line=words()["yes_page"]))  # her yes, back
        if link is not None:
            up.button = self.waiting.link(*link, now=self.instant)
            shown = _fenced(tool, _for_her_page(answer))
        return shown

    def _check(
        self, words: str, calls: list[str], up: _Up, *, skip: int, last: bool, said: str = ""
    ) -> str:
        """The note when his words would mislead her (core.talk.check): a
        tool's name, "done" with nothing written this message (or while a
        call waits for her Yes), or asking for a photo with no button for
        it. ``skip``: how many notes this message already sent; after one,
        only a card's number or a button that isn't there goes back
        (:func:`check.misleads`), and after two nothing does. ``last``: no
        step left to answer the note with."""
        if skip > 1 or last:
            return ""
        if skip:
            return check.misleads(words, button=up.button is not None, held=up.card is not None)
        wrote = any(
            tool in self.offered and hold.kind(self.offered[tool]) != hold.READ for tool in calls
        )
        mine = self._hers() if check.denies_hers(words) else ""
        if mine:
            return check.hers_note(mine)
        return check.problem(
            words,
            tools=self.offered,
            wrote=wrote,
            button=up.button is not None,
            held=up.card is not None,
            yes_back=up.button is not None and bool(up.button.get("again")),
            asked=hold.plain_decision(said),
            decided=hold.YES_PAGE in calls,
            said=said,
        )

    def _hers(self) -> str:
        """The first card waiting on her own yes, in her terms (waiting_cards'
        own ``about`` and ``yours``), or "" (the Haiku year's A09)."""
        if "waiting_cards" not in self.offered:
            return ""
        _shown, answer = _run(self.tools, "waiting_cards", {})
        rows = answer.get("rows") if isinstance(answer, dict) else None
        return next((str(r["about"]) for r in rows or [] if r.get("yours")), "")

    def _yes_page(
        self, args: dict, calls: list[str], nxt: dict | None, said: str
    ) -> Reply | tuple[str, Any]:
        """A card decision. Under the ministry's line, when her own message
        asks for it (``hold.asks_for``) and names one card, her word decides
        it by the decider's by-word rule, the connector door's; the words it
        weighs and keeps are hers (``said``), never the model's. Otherwise,
        and always for several cards at once, her own yes page, asked the
        way the month page's Approve button asks (``tap=True``). A link or
        an email ends the turn in code's words; anything else (decided by
        her word, already decided, someone else's phone confirms, a refusal)
        goes back to the model as data to put in words: (what it is shown,
        the answer when her word ran it, else None)."""
        from ..tools.decide import TAKE_BACK  # core.tools imports core.talk at load

        tools = self.tools
        verb = str(args.get("decision", ""))
        # a take-back is by her word alone: the decider holds it to her words asking for it
        by_word = args.get("cards") is None and (verb == TAKE_BACK or hold.asks_for(said, verb))
        try:
            out = tools.decider.decide(tools, {**args, "said": said}, tap=not by_word)
        except Exception as exc:  # noqa: BLE001  a refusal is the model's to put in plain words
            return _note(f"{hold.YES_PAGE} did not work: {type(exc).__name__}: {exc}"), None
        if isinstance(out, dict) and not out.get("vouched_by"):
            if out.get("link"):
                # kept for her button back only when her own words asked for it
                mine = hold.asks_for(said, verb)
                ask = {k: v for k, v in args.items() if k != "said"} if mine else None
                page = self.waiting.yes_page(
                    str(out["link"]), now=self.instant, card=args.get("card"), ask=ask
                )
                return Reply(words()["yes_page"], "ok", calls, nxt, waiting=page)
            if out.get("status") == "emailed":
                return Reply(words()["yes_emailed"], "ok", calls, nxt)
        return _fenced(hold.YES_PAGE, out), (out if by_word else None)
