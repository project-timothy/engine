"""The missionary's own setup questions, as pages on her phone (engine docs/
screens/README.md, screens 4 to 9; board c49, 2026-11-17).

``/start#<token>`` is the link a maintainer sends (``engine interview new``).
The fragment never reaches a server or a log: the page's one script reads
it and posts it to ``/start/next``, which draws the screen she is on. Each
answer posts to ``/start/answer`` (the letter's photo as a multipart form)
and draws the next one. The rest is plain HTML: no script, nothing loaded
from outside, a few kilobytes for a 2G phone. What she answers, and what is
kept, is core/onboarding/interview.py.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from html import escape
from typing import Any
from urllib.parse import parse_qs

from ..onboarding import interview as iv
from .receipts import shell

START_PATH = "/start"
NEXT_PATH = "/start/next"
ANSWER_PATH = "/start/answer"
PATHS = (NEXT_PATH, ANSWER_PATH)
MAX_FORM = 4096
MAX_BODY = iv.MAX_LETTER + 64 * 1024

_MORE = (
    # The receipt page draws every label as a button and hides every input
    # (its camera button); here a label is words, and its box shows.
    "label{background:none;color:var(--text);text-align:left;font-weight:400;padding:0;"
    "cursor:default}"
    "label.opt{border:1px solid var(--rule);border-radius:12px;padding:.7rem .8rem;"
    "margin:.5rem 0;cursor:pointer}"
    "label.opt b{font-weight:600}"
    "label.opt small{display:block;color:var(--text-2);font-size:.9rem;margin-left:1.6rem}"
    "label.opt input{display:inline-block;margin:0 .5rem 0 0;accent-color:var(--accent);"
    "width:1.1rem;height:1.1rem;vertical-align:-.15rem}"
    "label.field{margin:.8rem 0;font-weight:600}"
    "label.field input{display:block;width:100%;box-sizing:border-box;font:inherit;"
    "font-weight:400;margin-top:.35rem;padding:.6rem;border:1px solid var(--rule);"
    "border-radius:10px;background:var(--bg);color:var(--text)}"
    ".err{color:#c0392b;font-weight:600}"
    "button.link{background:none;color:var(--text-2);text-decoration:underline;"
    "font-weight:400;padding:.4rem 0}"
    "dl dt{color:var(--text-2);font-size:.9rem;margin-top:.7rem}"
    "dl dd{margin:0}"
    "h2{font-size:1.05rem;margin:1.1rem 0 .3rem}"
    "details summary{margin-top:.9rem;cursor:pointer}"
)


def csp(nonce: str, *, script: bool = False) -> str:
    """The start page runs its one script; every other page none. A form
    may post only here."""
    run = f"'nonce-{nonce}'" if script else "'none'"
    return (
        f"default-src 'none'; script-src {run}; style-src 'nonce-{nonce}'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


def _shell(title: str, body: str, nonce: str, script: str = "") -> str:
    return shell(title, body, nonce, more=_MORE, script=script)


_READ_HASH = (
    "var t=location.hash.slice(1);history.replaceState(null,'',location.pathname);"
    "if(t){var f=document.getElementById('go');f.token.value=t;f.submit();}"
)


def start_page(nonce: str) -> str:
    """``/start``: the link's fragment, posted, so it is never in a URL a
    server sees."""
    w = iv.words()
    body = (
        f'<form id="go" method=post action="{NEXT_PATH}"><input type=hidden name=token></form>'
        f"<p>{escape(w['title'])}...</p><noscript><p>{escape(w['timed_out'])}</p></noscript>"
    )
    return _shell(w["title"], body, nonce, _READ_HASH)


def _form(token: str, screen: str, inner: str, *, photo: bool = False) -> str:
    kind = ' enctype="multipart/form-data"' if photo else ""
    return (
        f'<form method=post action="{ANSWER_PATH}"{kind}>'
        f'<input type=hidden name=token value="{escape(token)}">'
        f'<input type=hidden name=screen value="{escape(screen)}">{inner}</form>'
    )


def _button(text: str, name: str = "", value: str = "", cls: str = "") -> str:
    named = f' name="{escape(name)}" value="{escape(value)}"' if name else ""
    styled = f' class="{cls}"' if cls else ""
    return f"<button{named}{styled}>{escape(text)}</button>"


def _field(name: str, label: str, value: str = "", kind: str = "text", hint: str = "") -> str:
    placeholder = f' placeholder="{escape(hint)}"' if hint else ""
    return (
        f'<label class="field">{escape(label)}<input type={kind} name="{name}" '
        f'value="{escape(value)}"{placeholder}></label>'
    )


def screen_html(book: iv.InterviewBook, token: str, screen: str, error: str = "") -> str:
    """One screen's body for ``token``, with what to ask when ``error``."""
    w = iv.words()
    person = book.person(token)
    answers = book.answers(token)
    by = iv._first(str(person.get("by", ""))) or "the person who sent this"
    err = f'<p class="err">{escape(error)}</p>' if error else ""
    if screen in ("done", "not_yet"):
        s = w[screen]
        says = s["church"] if screen == "not_yet" and answers.get("church") else s["says"]
        says = says.replace("{by}", by).replace("{name}", iv._first(str(person.get("name", ""))))
        return f"<h1>{escape(s['title'])}</h1><p>{escape(says)}</p>"
    s = w[screen] if screen != "heard" else w["heard"]
    head = f"<h1>{escape(s['ask'])}</h1>"
    if s.get("help"):
        head += f'<p class="note">{escape(s["help"])}</p>'
    if screen == "support":
        picked = set(answers.get("support", []))
        opts = "".join(
            f'<label class="opt"><input type=checkbox name=support value="{o["id"]}"'
            f"{' checked' if o['id'] in picked else ''}> <b>{escape(o['label'])}</b>"
            f"<small>{escape(o['note'])}</small></label>"
            for o in s["option"]
        )
        inner = opts + err + _button(w["continue"]) + _button(s["church"], "church", "yes", "link")
        return head + _form(token, screen, inner)
    if screen == "letter":
        inner = (
            f'<label class="field">{escape(s["photo"])}'
            '<input type=file name=photo accept="image/*" capture=environment></label>'
            + _field("legal_name", s["name"], str(answers.get("legal_name", "")))
            + err
            + _button(w["continue"])
            + f'<p class="note">{escape(s["cant_find"])}</p>'
        )
        return head + _form(token, screen, inner, photo=True)
    if screen == "where":
        so = iv.so_ill(answers)
        said = (
            f"<p>{escape(s['so'])}</p><ul>{''.join(f'<li>{escape(x)}</li>' for x in so)}</ul>"
            if so
            else ""
        )
        inner = (
            _field("town", s["town"], str(answers.get("town", "")), hint=s["example"])
            + err
            + said
            + _button(w["continue"])
        )
        return head + _form(token, screen, inner)
    if screen == "reviewer":
        r = answers.get("reviewer") or {}
        approve = answers.get("approves") == "approve"
        inner = (
            _field("reviewer_name", s["name"], str(r.get("name", "")))
            + _field("reviewer_email", s["email"], str(r.get("email", "")), kind="email")
            + f'<p class="note">{escape(s["promise"])}</p>'
            + f"<p>{escape(s['approves'])}</p>"
            + f'<label class="opt"><input type=radio name=approves value=look'
            f"{'' if approve else ' checked'}> {escape(s['look'])}</label>"
            + f'<label class="opt"><input type=radio name=approves value=approve'
            f"{' checked' if approve else ''}> {escape(s['approve'])}"
            f"<small>{escape(s['approve_note'])}</small></label>"
            + err
            + _button(w["continue"])
            + _button(s["nobody"], "nobody", "yes", "link")
        )
        return head + _form(token, screen, inner)
    if screen == "driving":
        inner = err + _button(s["yes"], "drives", "yes") + " " + _button(s["no"], "drives", "no")
        return head + _form(token, screen, inner) + f'<p class="note">{escape(s["later"])}</p>'
    heard = iv.heard(answers, name=str(person.get("name", "")))
    mine = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in heard["mine"])
    assumed = "".join(f"<li>{escape(x)}</li>" for x in heard["assumed"])
    change = "".join(
        _button(w[name]["title"], "change", name, "link") + "<br>" for name in iv.ORDER
    )
    inner = (
        f"<dl>{mine}</dl><h2>{escape(s['assume'])}</h2><ul>{assumed}</ul>"
        + err
        + _button(s["right"], "right", "yes")
        + f"<details><summary>{escape(s['change'])}</summary>{change}</details>"
    )
    return head + _form(token, screen, inner)


def read_back_html(token: str, legal_name: str, ein: str) -> str:
    """What her letter reads, for her tap (card R2): one plain line, and a
    form whose Yes posts the name and the number as her answer, the way she
    would have typed them. Her No leaves her the box she has today."""
    w = iv.words()
    s = w["letter"]
    says = s["reads"].replace("{name}", legal_name).replace("{ein}", ein)
    inner = (
        f'<input type=hidden name=legal_name value="{escape(legal_name)}">'
        f'<input type=hidden name=ein value="{escape(ein)}">'
        f"<p>{escape(says)}</p>"
        + _button(s["right"], "right", "yes")
        + _button(s["not_right"], "right", "no", "link")
    )
    return f"<h1>{escape(s['read_ask'])}</h1>" + _form(token, "letter", inner)


def _read_letter(
    book: iv.InterviewBook,
    token: str,
    fields: dict[str, Any],
    photo: bytes | None,
    letter: Callable[[bytes], Any] | None,
) -> str | None:
    """The letter screen's answer where the box reads the letter (card R2),
    as a page body; None to take her answer the way it always was. A photo
    with no name typed is read; what it reads comes back for her tap, and
    her photo is kept as it always is. Her "No" brings back the box to type
    in. Nothing read is kept until her yes posts it as her answer."""
    w = iv.words()["letter"]
    if fields.get("right") == "no":
        return screen_html(book, token, "letter", w["type_it"])
    if letter is None or fields.get("right") or str(fields.get("legal_name", "")).strip():
        return None
    if not photo:
        return None
    if len(photo) > iv.MAX_LETTER:
        return None  # too big: she hears so, as today
    reading = letter(photo)
    if reading is None or book.keep_letter(token, photo):
        return None
    return read_back_html(token, reading.legal_name, reading.ein)


def _fields(headers: Any, raw: bytes) -> tuple[dict[str, Any], bytes | None]:
    """A posted form's fields (a repeated name as a list) and its photo."""
    kind = str(headers.get("Content-Type", ""))
    fields: dict[str, Any] = {}
    photo: bytes | None = None
    if kind.startswith("multipart/form-data"):
        message = BytesParser(policy=policy.HTTP).parsebytes(
            b"Content-Type: " + kind.encode() + b"\r\n\r\n" + raw
        )
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if name == "photo":
                photo = payload or None
            elif name:
                _add(fields, str(name), payload.decode("utf-8", "replace"))
    else:
        for key, values in parse_qs(raw.decode("utf-8", "replace")).items():
            for value in values:
                _add(fields, key, value)
    return fields, photo


def _add(fields: dict[str, Any], key: str, value: str) -> None:
    if key in fields:
        held = fields[key]
        fields[key] = [*held, value] if isinstance(held, list) else [held, value]
    else:
        fields[key] = value


def answer(
    book: iv.InterviewBook | None,
    path: str,
    headers: Any,
    raw: bytes,
    origin: str,
    now: Callable[[], float] = time.time,
    letter: Callable[[bytes], Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One form post to the interview, from this box's own pages: (status,
    headers, body). ``letter`` reads her IRS letter's photo where the box's
    file switches the reader on (card R2); None leaves the screen as it was."""
    nonce = secrets.token_urlsafe(16)
    w = iv.words()
    given = headers.get("Origin")
    status = 403
    body = f"<h1>{escape(w['title'])}</h1><p>{escape(w['timed_out'])}</p>"
    if book is not None and (not given or given == origin):
        fields, photo = _fields(headers, raw)
        token = str(fields.get("token", ""))
        screen = book.next_screen(token, now=now())
        status = 400 if screen is None else 200
        if screen is not None:
            error = ""
            answering = path.split("?")[0] == ANSWER_PATH
            read_back = (
                _read_letter(book, token, fields, photo, letter)
                if answering and screen == "letter" and fields.get("screen") == "letter"
                else None
            )
            if read_back is None and answering:
                error = book.take(
                    token, str(fields.get("screen", "")), fields, photo=photo, now=now()
                )
                screen = book.next_screen(token, now=now()) or screen
            body = read_back or screen_html(book, token, screen, error)
    data = _shell(w["title"], body, nonce).encode("utf-8")
    out = {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": csp(nonce),
        "Referrer-Policy": "same-origin",
        "Cache-Control": "no-store",
        "Content-Length": str(len(data)),
    }
    return status, out, data


__all__ = [
    "ANSWER_PATH",
    "MAX_BODY",
    "NEXT_PATH",
    "PATHS",
    "START_PATH",
    "answer",
    "csp",
    "read_back_html",
    "screen_html",
    "start_page",
]
