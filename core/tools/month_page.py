"""The month page: a reviewer who doesn't use an AI sees a person's month on
their phone (t-month-page, the owner's yes of 2026-10-14).

The "month is ready" email links to the doorkeeper's ``/month`` page, which
asks for the reviewer's own Face ID every time it opens, then posts a
one-use grant here (``SHOW_PATH``) in a form, so it never sits in a URL or a
log. The engine spends the grant with the doorkeeper (localhost, the box's
shared secret) and learns only who is looking at whose month; the doorkeeper
never holds a figure. The month is the one ``month_report`` gives that
reviewer, under their own rules (``Viewer``): receipts, drives, bank lines,
the total, and where the review stands. Plain HTML, escaped here, no script.

When the month's card waits on this reviewer, an Approve button asks for
their yes the usual way (core/tools/decide.py): a witness link, Face ID, and
the tap finishes it; with no passkey, the link comes to their email. The
button's form carries a signature, alive ``BUTTON_TTL``, naming the reviewer
and the card; it never decides by word.
Standard library only (invariant 9).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import tomllib
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from html import escape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs

from ..agents.expenses.records import REVIEW_CARD
from ..agents.expenses.statements import SPENDING
from ..engine import private_file
from .receipts import shell
from .viewer import ViewerError, display_name, first_name, unit_of

SHOW_PATH = "/month/show"
APPROVE_PATH = "/month/approve"
PATHS = frozenset({SHOW_PATH, APPROVE_PATH})
MAX_FORM = 4096
BUTTON_TTL = 15 * 60
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
WORDS_FILE = Path(__file__).with_name("month_page.toml")

_AGAIN = "Open the link in your email again; your Face ID opens the month."
_EMAILED = (
    "Tim emailed you a link to finish your yes. Open it on any phone or computer and tap "
    "the button."
)


class GrantDesk(Protocol):
    def spend_month(self, grant: str) -> tuple[str, str, str] | None:
        """(reviewer, person, month) behind a grant, once."""
        ...


def csp(nonce: str) -> str:
    """No script at all; the page's own style; a form may post only here."""
    return (
        f"default-src 'none'; script-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


def answer(
    page: MonthPage | None, path: str, headers: Any, raw: bytes, origin: str
) -> tuple[int, dict[str, str], bytes]:
    """One form post to the month page, from the box's door: (status,
    headers, body). Only this box's own pages may post here, and a box with
    no doorkeeper shows no month."""
    nonce = secrets.token_urlsafe(16)
    given = headers.get("Origin")
    status, location = 403, ""
    html = _shell(
        "Open it from your email", f"<h1>Open it from your email</h1><p>{_AGAIN}</p>", "", nonce
    )
    if page is not None and (not given or given == origin) and len(raw) <= MAX_FORM:
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if path.split("?")[0] == SHOW_PATH:
            status, html = page.show(form, nonce)
        else:
            status, location, html = page.approve(form, nonce)
    data = html.encode("utf-8")
    out = {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": csp(nonce),
        # same-origin: the Approve form must carry this box's Origin, never
        # "null" (what a browser sends under no-referrer), or it is refused.
        "Referrer-Policy": "same-origin",
        "Cache-Control": "no-store",
        "Content-Length": str(len(data)),
    }
    if location:
        out["Location"] = location
    return status, out, data


def _signing_key(key_file: Path | None) -> bytes:
    """The key the page's buttons are signed with. In ``key_file`` (beside
    the books, private to the box's account), so a room that naps and wakes
    (shot 4, card H3) still honors the buttons on a reviewer's open page;
    with none, a fresh one each start, and a restart retires every button."""
    if key_file is None:
        return secrets.token_bytes(32)
    if key_file.is_file():
        return bytes.fromhex(key_file.read_text(encoding="utf-8").strip())
    key = secrets.token_bytes(32)
    private_file.keep(key_file, key.hex())
    return key


def page_for(
    desk: Any, *, tools_for: Callable[[str], Any], ministry: str, key_file: Path | None = None
) -> MonthPage | None:
    """The month page, on a box whose doorkeeper can vouch for a reviewer."""
    if not desk:
        return None
    return MonthPage(desk=desk, tools_for=tools_for, ministry=ministry, key_file=key_file)


class MonthPage:
    def __init__(
        self,
        *,
        desk: GrantDesk,
        tools_for: Callable[[str], Any],
        ministry: str = "",
        now: Callable[[], float] = time.time,
        key_file: Path | None = None,
    ) -> None:
        self.desk, self.tools_for, self.ministry, self.now = desk, tools_for, ministry, now
        self._key = _signing_key(key_file)

    # ---- showing the month

    def show(self, form: dict[str, str], nonce: str = "") -> tuple[int, str]:
        nonce = nonce or secrets.token_urlsafe(16)
        found = self.desk.spend_month(form.get("grant", ""))
        if found is None:
            return 400, self._note("This page has timed out", _AGAIN, nonce)
        reviewer, person, month = found
        if not _MONTH.match(month) or not person:
            return 400, self._note("This page has timed out", _AGAIN, nonce)
        try:
            t = self.tools_for(reviewer)
        except ViewerError:
            return 403, self._note("Not yours to see", "This month isn't one you look over.", nonce)
        if not t.sees("expense.report", person):
            return 403, self._note("Not yours to see", "This month isn't one you look over.", nonce)
        from .catalog import _month_report, _review_stands

        mine = [
            p
            for p in _month_report(t, {"month": month})["people"]
            if unit_of(p["person"]) == person
        ]
        shown = mine[0] if mine else _nothing(person, month, _review_stands(t, month))
        when = f"{datetime.strptime(month + '-01', '%Y-%m-%d'):%B %Y}"
        title = f"{display_name(person)}'s {when}"
        card = self._card(t, person, month)
        approve = self._button(reviewer, card) if card is not None else None
        page = render(
            shown,
            title=title,
            when=when,
            ministry=self.ministry,
            approve=approve,
            nonce=nonce,
            card=_review(t, person, month),
        )
        return 200, page

    def _card(self, t: Any, person: str, month: str) -> int | None:
        """The month's review card, when it waits on this reviewer's yes."""
        decider = getattr(t, "decider", None)
        if decider is None or not decider.offered():
            return None
        rows = t._rows(
            "SELECT id, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
            "AND status = 'pending' ORDER BY id DESC",
            (t.tenant, REVIEW_CARD),
        )
        for r in rows:
            params = json.loads(r["params_json"] or "{}")
            if params.get("month") == month and unit_of(str(params.get("person", ""))) == person:
                return int(r["id"]) if decider.may_tap(t, int(r["id"])) else None
        return None

    # ---- the Approve button

    def _sign(self, who: str, card: str, until: str) -> str:
        said = f"{who}\n{card}\n{until}".encode()
        return hmac.new(self._key, said, hashlib.sha256).hexdigest()

    def _button(self, who: str, card: int) -> dict[str, str]:
        until = str(int(self.now()) + BUTTON_TTL)
        return {
            "who": who,
            "card": str(card),
            "until": until,
            "sig": self._sign(who, str(card), until),
        }

    def approve(self, form: dict[str, str], nonce: str = "") -> tuple[int, str, str]:
        """(status, where to send the browser, the page when it stays)."""
        nonce = nonce or secrets.token_urlsafe(16)
        who, card, until = form.get("who", ""), form.get("card", ""), form.get("until", "")
        good = hmac.compare_digest(form.get("sig", ""), self._sign(who, card, until))
        if not good or not card.isdigit() or not until.isdigit() or int(until) < self.now():
            return 400, "", self._note("This page has timed out", _AGAIN, nonce)
        try:
            t = self.tools_for(who)
            out = t.decider.decide(t, {"card": int(card), "decision": "approve"}, tap=True)
        except (ValueError, ViewerError) as exc:
            return 400, "", self._note("Not approved", str(exc), nonce)
        if out.get("link"):
            return 303, str(out["link"]), ""
        if out.get("status") == "emailed":  # no passkey: the link went to their email
            return 200, "", self._note("Check your email", _EMAILED, nonce)
        return 200, "", self._note("Your yes", _plain(str(out.get("say", ""))), nonce)

    def _note(self, title: str, words: str, nonce: str) -> str:
        return _shell(
            title, f"<h1>{escape(title)}</h1><p>{escape(words)}</p>", self.ministry, nonce
        )


def _review(t: Any, person: str, month: str) -> dict | None:
    """The month's newest review card for the person, waiting or approved:
    the one place the rules' check was worked out. None before it's sent."""
    rows = t._rows(
        "SELECT params_json, status, resolved_at FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? "
        "AND status IN ('pending', 'approved') ORDER BY id DESC",
        (t.tenant, REVIEW_CARD),
    )
    for r in rows:
        params = json.loads(r["params_json"] or "{}")
        if params.get("month") == month and unit_of(str(params.get("person", ""))) == person:
            if r["status"] == "approved":
                params["approved_on"] = str(r["resolved_at"] or "")
            return params
    return None


def without_you(card: dict | None, when: str) -> str:
    """Where a month approver was named and the person approved their own
    month instead, the words that tell the approver so (board c48, the
    owner's decision of 2026-11-17); "" otherwise. The card says it: only a
    ministry that asked to hear of it stamps a yes as someone's own."""
    if not card or card.get("self_approved") != "true":
        return ""
    who = str(card.get("person", ""))
    if unit_of(str(card.get("decided_by", ""))) != unit_of(who):
        return ""
    words = _words()["without_you"].replace("{who}", first_name(who))
    words = words.replace("{month}", _month_name(when))
    return words.replace("{day}", _on(str(card.get("approved_on", ""))))


def _on(iso: str) -> str:
    """ "2026-11-20T..." reads "November 20"; "" when it can't be read."""
    try:
        day = datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{day:%B} {day.day}"


# ---- what Tim checked (docs/screens/README.md, R4)


@cache
def _words() -> dict:
    with WORDS_FILE.open("rb") as handle:
        return tomllib.load(handle)


def all_words() -> list[str]:
    """Every string a reviewer may read from the words file."""
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(_words())
    return out


def _one_or_many(pair: list[str], n: int, **fill: str) -> str:
    return (pair[0] if n == 1 else pair[1]).replace("{n}", str(n)).format_map(_Keep(fill))


class _Keep(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _listed(parts: list[str]) -> str:
    """ "3 receipts, 2 drives and 1 bank line"."""
    if len(parts) < 2:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {_words()['and']} {parts[-1]}"


def counts(person: dict) -> str:
    said = _words()["count"]
    parts = []
    for key, rows in (
        ("receipt", person.get("lines", [])),
        ("drive", person.get("drives", [])),
        ("bank", person.get("bank", [])),
    ):
        if rows:
            parts.append(f"{len(rows)} {said[key][0 if len(rows) == 1 else 1]}")
    return _listed(parts)


def checks(person: dict, card: dict | None) -> list[tuple[str, str]]:
    """(``yes`` | ``no`` | ``none``, the words) for each thing Tim checked,
    in the reviewer's order: receipts, the ministry's rules, the bank."""
    w, who = _words(), first_name(str(person.get("person", "")))
    out: list[tuple[str, str]] = []
    bank = person.get("bank", [])
    missing = sum(1 for b in bank if b.get("role") in SPENDING and not b.get("receipt"))
    out.append(
        ("no", _one_or_many(w["receipts"]["no"], missing))
        if missing
        else ("yes", w["receipts"]["yes"])
    )
    if card is not None:
        said = card.get("policy")
        if said is None:
            out.append(("none", w["rules"]["none"]))
        elif said == "none" and not person.get("rules_say"):
            out.append(("yes", w["rules"]["yes"]))
        else:
            out.append(("no", w["rules"]["no"]))
    waiting = int(person.get("bank_waiting", 0) or 0)
    if not bank:
        out.append(("none", w["bank"]["none"]))
    elif waiting:
        out.append(("no", _one_or_many(w["bank"]["no"], waiting, who=who)))
    else:
        out.append(("yes", w["bank"]["yes"]))
    return out


def worth_a_look(person: dict) -> str:
    """The one thing a reviewer should look at first: what the ministry's
    rules say about the month, else cash held past its nudge day; else
    nothing."""
    said = person.get("rules_say") or []
    if said:
        return str(said[0])
    return str(person.get("cash_oldest") or "")


_MARKS = {"yes": "\u2713", "no": "!", "none": "\u00b7"}


def _plain(say: str) -> str:
    """The decider's words meant for an AI, as a sentence for a person."""
    if say.startswith("They've tapped"):
        return "Your yes is being written now. You can close this page."

    if say.startswith("It's already done."):
        return say.removeprefix("It's already done.").strip() or "It's already done."
    return "It's already done."


def _nothing(person: str, month: str, stands: dict[str, str]) -> dict:
    review = next((s for p, s in stands.items() if unit_of(p) == person), "")
    return {
        "person": display_name(person),
        "total": "0.00",
        "review": review or "Not yet sent for review",
        "lines": [],
        "drives": [],
        "bank": [],
        "empty": True,
    }


def _dollars(value: Any) -> str:
    try:
        return f"${Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return escape(str(value))


def _day(iso: Any) -> str:
    """2026-10-15 reads as "Oct 15", the way the yes page says it."""
    try:
        day = datetime.strptime(str(iso), "%Y-%m-%d")
    except ValueError:
        return str(iso or "")
    return f"{day:%b} {day.day}"


def _li(parts: list[str], waiting: Any = None) -> str:
    said = " · ".join(escape(p) for p in parts if p)
    wait = f'<br><span class="note">Waiting on {escape(str(waiting))}</span>' if waiting else ""
    return f"<li>{said}{wait}</li>"


def _receipt(ln: dict) -> str:
    money = _dollars(ln["dollars"]) if ln.get("dollars") is not None else ""
    said = str(ln.get("said") or "")
    parts = [money, said if said and said != money else "", ln.get("category") or ""]
    parts += [ln.get("fund") or "", _day(ln.get("date"))]
    rate = f'<br><span class="note">{escape(ln["rate"])}</span>' if ln.get("rate") else ""
    return _li([str(p) for p in parts], ln.get("waiting_on")).replace("</li>", rate + "</li>")


def _drive(d: dict) -> str:
    money = _dollars(d["dollars"]) if d.get("dollars") else ""
    mine = "your own" if d.get("ministry") is False else ""
    parts = [money, f"{d.get('miles', '')} miles", d.get("for") or mine, _day(d.get("date"))]
    return _li([str(p) for p in parts], d.get("waiting_on"))


def _bank(b: dict) -> str:
    money = _dollars(b["dollars"]) if b.get("dollars") is not None else ""
    parts = [
        money,
        b.get("words") or "",
        b.get("for") or b.get("category") or "",
        _day(b.get("date")),
    ]
    return _li([str(p) for p in parts], b.get("waiting_on"))


_MORE = """h2{font-size:1.05rem;margin:1.2rem 0 .3rem}
.total{font-size:1.6rem;font-weight:700;margin:.2rem 0}
li{margin:.45rem 0}
form{margin-top:1.2rem}
ul.checked{list-style:none;padding-left:0}
.mark{display:inline-block;width:1.3rem;font-weight:700}
li.no .mark{color:#a33}
.look{border-left:3px solid #c90;padding:.2rem .7rem}"""


def render(
    person: dict,
    *,
    title: str,
    ministry: str,
    approve: dict[str, str] | None,
    nonce: str,
    when: str = "this month",
    card: dict | None = None,
) -> str:
    """One person's month, every word escaped here. ``card``: the month's
    review card, whose rules' check the page says back (None: not sent)."""
    w = _words()
    body = [f"<h1>{escape(title)}</h1>"]
    body.append(f"<p>Where it stands: <b>{escape(str(person.get('review', '')))}</b></p>")
    alone = without_you(card, when)
    if alone:
        body.append(f'<p class="alone">{escape(alone)}</p>')
    lines, drives, bank = person.get("lines", []), person.get("drives", []), person.get("bank", [])
    if person.get("empty") or not (lines or drives or bank):
        body.append(f"<p>Nothing in {escape(when)} yet.</p>")
    else:
        who = first_name(str(person.get("person", "")))
        body.append(f"<p>{escape(w['owed'].replace('{who}', who))}</p>")
        body.append(f'<p class="total">{_dollars(person.get("total", "0"))}</p>')
        body.append(f'<p class="note">{escape(counts(person))}</p>')
        said = "".join(
            f'<li class="{mark}"><span class="mark" aria-hidden="true">{_MARKS[mark]}</span> '
            f"{escape(words)}</li>"
            for mark, words in checks(person, card)
        )
        body.append(f'<h2>{escape(w["heading"])}</h2><ul class="checked">{said}</ul>')
        look = worth_a_look(person)
        if look:
            body.append(
                f'<div class="look"><h2>{escape(w["look"])}</h2><p>{escape(look)}</p></div>'
            )
        waiting = person.get("waiting_on_a_rate", 0) + person.get("bank_waiting", 0)
        if waiting:
            body.append(f'<p class="note">{waiting} still waiting, so not in the total yet.</p>')
        rules = person.get("rules_say", [])
        if rules:
            said = "".join(f"<li>{escape(str(r))}</li>" for r in rules)
            body.append(f"<h2>What the ministry's rules say</h2><ul>{said}</ul>")
        for heading, rows, one in (
            ("Receipts", lines, _receipt),
            ("Drives", drives, _drive),
            ("Bank lines", bank, _bank),
        ):
            if rows:
                body.append(f"<h2>{heading}</h2><ul>{''.join(one(r) for r in rows)}</ul>")
    if approve:
        fields = "".join(
            f'<input type=hidden name="{k}" value="{escape(v)}">' for k, v in approve.items()
        )
        body.append(
            f'<form method=post action="{APPROVE_PATH}">{fields}'
            f"<button>{escape(w['approve'].replace('{month}', _month_name(when)))}</button></form>"
            '<p class="note">Next, your phone asks for your Face ID, so it\'s you saying yes.</p>'
        )
    return _shell(title, "".join(body), ministry, nonce)


def _month_name(when: str) -> str:
    """ "October 2026" reads "October"."""
    head, _, year = when.rpartition(" ")
    return head if year.isdigit() and head else when


def _shell(title: str, body: str, ministry: str, nonce: str) -> str:
    return shell(title, body, nonce, ministry=ministry, more=_MORE)


__all__ = [
    "APPROVE_PATH",
    "BUTTON_TTL",
    "PATHS",
    "SHOW_PATH",
    "MonthPage",
    "all_words",
    "checks",
    "counts",
    "answer",
    "page_for",
    "csp",
    "render",
    "worth_a_look",
]
