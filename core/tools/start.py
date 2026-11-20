"""start_here: the one next thing waiting on the person, who they are, and
Tim's own answers to how-do-I questions. Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..authority import Request, evaluate
from .reads import _log
from .receipts import _PLACERS
from .viewer import display_name

if TYPE_CHECKING:
    from .catalog import Tools


def _next_and_first(t: Tools, *, explain: bool = True) -> tuple[dict, dict | None]:
    """The one next thing for the person, and the first-time words due with
    it, from one read of their books."""
    from .first_time import closing
    from .next_thing import of

    log = _log(t)
    nxt = of(t, log)
    return nxt, closing(t, log, nxt) if explain else None


def _faq_answers(t: Tools, about: list[str]) -> tuple[list[dict], list[dict]]:
    """Tim's answers for this person's tools, in two parts that fit one reply
    (pass^5 on 2026-11-17: all 97 answers ran 65 KB, too big for the
    person's AI to take in one piece): asked about by id, those answers in
    full; else the answers for everyone in full and the rest by question."""
    from ..capabilities.faq import faq_for

    entries = faq_for(t._specs, voice=bool(t.talk_page))
    if about:
        return [e for e in entries if e["id"] in about], []
    return [e for e in entries if not e["covers"]], [
        {"id": e["id"], "ask": e["ask"]} for e in entries if e["covers"]
    ]


def _start_here(t: Tools, args: dict) -> dict:
    """Who the person is and Tim's answers for the tools they have (the 10/14
    phone run: with no way to ask, Claude guessed who the reviewer was)."""
    answers, more = _faq_answers(t, [str(a) for a in args.get("about") or []])
    faq = {"answers": answers, **({"more": more} if more else {})}
    if t.viewer is None:
        return {"you": {"ministry": t.ministry}, **faq}
    policy, me = t.viewer.policy, t.viewer.person

    def files(who: str) -> bool:
        return evaluate(policy, Request(who, "submit", "expense.report", scope=who)).allowed

    def looks_over(who: str, whom: str) -> bool:
        return any(
            evaluate(policy, Request(who, action, "expense.report", scope=whom)).allowed
            for action in _PLACERS
        )

    others = [p for p in policy.people if p != me]
    held = policy.people[me].roles if me in policy.people else ()
    seen = [p for p in others if evaluate(policy, Request(me, "view", "people", scope=p)).allowed]
    nxt, first = _next_and_first(t)
    # Where Tim's own voice is on, his page (card T2): the person's AI can
    # point them there, and nothing else is needed to talk to him.
    own = {"talk_page": t.talk_page} if t.talk_page else {}
    return {
        "next": nxt,
        **({"first_time": first} if first is not None else {}),
        "you": {
            "name": display_name(me),
            "does": list(held),
            "ministry": t.ministry,
            "sends_receipts": bool(t.receipts is not None and t.receipts.offered()),
            "looks_over": [display_name(p) for p in others if files(p) and looks_over(me, p)],
            "looked_over_by": [display_name(p) for p in others if files(me) and looks_over(p, me)],
            "sees_people": [
                {"name": display_name(p), "does": list(policy.people[p].roles)} for p in seen
            ],
            **own,
        },
        **faq,
    }
