"""The one next thing waiting on a person (docs/screens/README.md, chunk 1).

The owner, 2026-11-16: "the complexity is real and the computer hides it." A
missionary does not know what Tim can do and will never read the FAQ, so Tim
leads. This reads the person's books and names the single most useful thing
waiting on them, in plain words, with what it takes and the tool that does it.
``start_here`` says it first; every answer through the door ends with it, or
with "Nothing's waiting on you." Boundary classification
(docs/boundary-rules.md): **code**. No model picks it, and it writes nothing.

In rank order, the first that holds:

1. ``yes``: a card waiting on their yes (the routing lane's own rule for who a
   card waits on, the one the morning email uses).
2. ``cash``: their oldest ATM cash has reached the nudge day of their cash
   clock (45 of 60 unless an adopted plan says), the one count_cash settles.
3. ``whose_statement``: a statement they sent waits on whose account it is
   (the one shorter question when its name says whose, t-account-from-name).
4. ``month_ready``: a month gone by, not yet sent, with nothing in it
   waiting on a day, an amount, a rate, or a bank line's word.
5. ``first_statement``: someone who files their own month has no bank
   statement in: none of their own, and, for whoever looks over every
   month, none of the ministry's own account from anyone.
6. ``statement``: last month's statement isn't in, when one was before.

Else ``nothing``. Words live in ``next_thing.toml``; every figure in them
(the cash, its day and age) comes from the books.
"""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

from ..agents.expenses import statements
from ..agents.expenses.records import DATED_EVENT, DRIVE_EVENT, LANDED_EVENT
from ..authority import UNDESCRIBED, Request, card_request, evaluate
from ..engine.registry import load_card_authority
from .viewer import display_name, unit_of

WORDS_FILE = Path(__file__).with_name("next_thing.toml")

RANK = (
    "yes",
    "cash",
    "whose_statement",
    "month_ready",
    "first_statement",
    "statement",
)
"""Every kind of next thing, first to last."""

NOT_SENT = "Not yet sent for review"  # month_report's review for a month with no card
RATE = "the day's exchange rate"  # a line the month's own run prices; never waits on them


@cache
def _file() -> dict:
    return tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))


WORDS: dict[str, str] = dict(_file()["words"])
TAKES: dict[str, str] = dict(_file()["takes"])


def of(t: Any, log: list[dict] | None = None) -> dict:
    """The one next thing for the person ``t`` answers as (core/tools/catalog.Tools),
    from their books' event log (read here when not given)."""
    from .catalog import _log

    log = _log(t) if log is None else log
    for find in (_yes, _cash, _whose_statement, _month_ready, _statement):
        found = find(t, log)
        if found is not None:
            return found
    return {"kind": "nothing", "say": WORDS["nothing"]}


def _thing(kind: str, say: str, tool: str, args: dict | None = None) -> dict:
    return {"kind": kind, "say": say, "takes": TAKES[kind], "tool": tool, "args": args or {}}


def _yes(t: Any, _log: list[dict]) -> dict | None:
    """The cards waiting on this person's own yes, oldest first."""
    from ..engine.authority_gate import waiting
    from ..ledger import Ledger
    from .catalog import _card_visible
    from .decide import card_words

    policy, me = t.viewer.policy, t.viewer.person
    con = t._db()
    try:
        items = waiting(
            policy,
            Ledger(Path(t.ledger_root), con),
            t.tenant,
            today=t._today(),
            now=datetime.now(UTC).isoformat(),
        )
        agents = {r["id"]: r["agent"] for r in con.execute("SELECT id, agent FROM approval_queue")}
    finally:
        con.close()
    mine = []
    for item in items:
        agent = str(agents.get(item["card"], ""))
        rule = load_card_authority(agent).get(str(item["action_type"]), UNDESCRIBED)
        row = {"agent": agent, "action_type": item["action_type"], "params": item["params"]}
        if (
            me in item["waiting_on"]
            and evaluate(policy, card_request(rule, me, item["params"], policy)).allowed
            and _card_visible(t, {**row, "params_json": _json(item["params"])})
        ):
            mine.append(row)
    if not mine:
        return None
    about = card_words(mine[0])
    about = about[:1].upper() + about[1:]
    say = (
        WORDS["yes_more"].format(about=about, more=len(mine) - 1)
        if len(mine) > 1
        else WORDS["yes"].format(about=about)
    )
    return _thing("yes", say, "waiting_cards")


def _json(params: dict) -> str:
    import json

    return json.dumps(params)


def _cash(t: Any, log: list[dict]) -> dict | None:
    """Their oldest ATM cash, once it reaches the nudge day of their clock."""
    from .catalog import _clock

    me = t.viewer.person
    today = t._today().isoformat()
    clock = _clock(t, display_name(me))
    for (whose, cur), (day, left) in sorted(statements.cash_age(log, today).items()):
        old = statements.days_old(day, today)
        if unit_of(whose) != me or old < clock.nudge:
            continue
        words = WORDS["cash_past"] if old > clock.due else WORDS["cash"]
        say = words.format(
            cash=statements.cash_words(f"{cur}{left}"),
            day=_day(day),
            old=old,
            due=clock.due,
        )
        return _thing("cash", say, "count_cash")
    return None


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}"


def _files(t: Any) -> bool:
    """Whether the person files their own month (whose statements and months these are)."""
    policy, me = t.viewer.policy, t.viewer.person
    return evaluate(policy, Request(me, "submit", "expense.report", scope=me)).allowed


def _whose_statement(t: Any, _log: list[dict]) -> dict | None:
    if "bank_statement" not in t._specs or t.receipts is None:
        return None
    held = t.receipts.desk.held(t.viewer.person)
    if not held:
        return None
    return _thing("whose_statement", _whose_words(t, held), "bank_statement")


def _whose_words(t: Any, held: list) -> str:
    """The shorter question when every statement waiting on whose account
    it is has a name that says so (t-account-from-name), else the question
    as always. Her yes comes first: the next thing never settles one."""
    from .receipts import STATEMENT
    from .statement_names import ask

    waiting = [h for h in held if h.kind == STATEMENT and h.flagged is None]
    named = t.receipts.named(waiting)
    if not waiting or len(named) < len(waiting):
        return WORDS["whose_statement"]
    sides = list(named.values())
    question = ask(sides.count(statements.MINISTRY), sides.count(statements.OWN))
    return WORDS["whose_named"].format(ask=question)


def _theirs(t: Any, log: list[dict]) -> list[dict]:
    """The person's own records in the event log: receipts, drives, bank lines."""
    me = t.viewer.person
    return [e for e in log if unit_of(str(e["payload"].get("person", ""))) == me]


def _month_of(e: dict, dated: dict[str, str]) -> str:
    """The month a record is in: a receipt's own day when it was said, as
    month_report files it."""
    p = e["payload"]
    if e["event_type"] == LANDED_EVENT:
        return dated.get(str(p.get("sha256")), "")[:7] or str(p.get("month") or "")
    if e["event_type"] == DRIVE_EVENT:
        return str(p.get("start") or "")[:7]
    if e["event_type"] == statements.LINE_EVENT:
        return str(p.get("day") or "")[:7]
    return ""


def _month_ready(t: Any, log: list[dict]) -> dict | None:
    """The oldest month gone by that is theirs, unsent, and waits on nothing."""
    from .catalog import _month_report

    if "send_month_for_review" not in t._specs or not _files(t):
        return None
    now = t._today().strftime("%Y-%m")
    dated = {
        str(e["payload"].get("sha256")): str(e["payload"].get("day") or "")
        for e in log
        if e["event_type"] == DATED_EVENT
    }
    months = sorted({m for e in _theirs(t, log) if (m := _month_of(e, dated)) and m < now})
    name = display_name(t.viewer.person)
    for month in months:
        people = _month_report(t, {"month": month, "person": name})["people"]
        if not people:
            continue
        p = people[0]
        if p["review"] != NOT_SENT or not (p["lines"] or p["drives"] or p["bank"]):
            continue
        if p["bank_waiting"] or any(
            ln["dollars"] is None and ln.get("waiting_on") != RATE for ln in p["lines"]
        ):
            continue
        said = f"{date.fromisoformat(month + '-01'):%B}"
        return _thing(
            "month_ready",
            WORDS["month_ready"].format(month=said),
            "send_month_for_review",
            {"month": month},
        )
    return None


def _statement(t: Any, log: list[dict]) -> dict | None:
    """Their first bank statement, or last month's, through the catch-up link."""
    from .catalog import _sees_every_month

    if "catch_up_link" not in t._specs or not _files(t):
        return None
    lines = [e for e in log if e["event_type"] == statements.LINE_EVENT]
    mine = {id(e) for e in _theirs(t, lines)}
    # Whoever looks over every month runs the ministry's books: its own
    # account's statement counts, whoever sent it (the president of a small
    # ministry, Caleb's box, 2026-11-16).
    keeps_books = _sees_every_month(t)
    days = [
        str(e["payload"].get("day") or "")
        for e in lines
        if id(e) in mine or (keeps_books and e["payload"].get("account") == statements.MINISTRY)
    ]
    if not days:
        return _thing("first_statement", WORDS["first_statement"], "catch_up_link")
    first = t._today().replace(day=1)
    last = (
        first.replace(year=first.year - 1, month=12)
        if first.month == 1
        else first.replace(month=first.month - 1)
    )
    month = last.strftime("%Y-%m")
    if min(days)[:7] > month or any(d.startswith(month) for d in days):
        return None
    return _thing("statement", WORDS["statement"].format(month=f"{last:%B}"), "catch_up_link")


__all__ = ["RANK", "TAKES", "WORDS", "WORDS_FILE", "of"]
