"""Tim's own page, where a person talks to him (Standalone Tim, shot 1,
card T1, 2026-11-17).

``/talk`` is one page on her phone, built to the rule every Tim page keeps:
rendered here, a few kilobytes, nothing loaded from anywhere else, and one
post per message. Where Tim's voice is on, its one script, under the
response's nonce, only shows
that Tim is working the moment she taps Send and keeps a second tap from
sending it again (the second live trial, 2026-11-18: 5 to 7 seconds with
nothing on screen, so she tapped again and got two answers). The page works
the same without it: the box itself answers a repeat of her last words,
sent while Tim was still on them, with the reply he already gave
(``REPEAT_SECONDS``). The one thing waiting on her sits on top (code's, from
her books: ``next_thing.of``), then the last few exchanges, then one box to
type or dictate in and one Send. ``POST /talk/say`` runs Tim's reply
(``core.talk.reply``) with her own tools, the same ones the door builds,
and answers 303 back to the page, so a reload never says it twice. A photo
button (``POST /talk/photo``) hands her a one-use receipt ticket and sends
her to the receipt page she already knows (core/tools/receipts.py).

Who is asking is the browser the doorkeeper already remembers. Its sign-in
left a cookie on this same origin; the engine never reads that cookie on
its own: it asks the doorkeeper whose it is (``WitnessDesk.session_person``,
the doorkeeper's engine-only ``/session/who``). That browser's power is the
doorkeeper's own rule: it reads and talks, and never says yes to a card.
When Tim would make a decision, or file an amount he inferred, the call
waits under his message as Yes and Not now (card V2, ``POST /talk/yes``):
her tap runs exactly that call, once. A card's own yes is the button to
her yes page, the doorkeeper's, Face ID or the emailed link. A
browser it doesn't remember sees one sign-in here: the doorkeeper's Face ID
step that "Finish setting up" uses (``/setup/start/*``), which remembers the
browser. Someone with no passkey on this phone is pointed at their welcome
email, which signs a browser in without one.

Where his voice is on (card T2), the page tells setup two things: her
first word to Tim (setup's "where do you want to talk to me" step) and the
page opening from her home screen (its manifest starts at ``/talk?home``).

The conversation stays in her own files beside the books (``core.talk``);
nothing here writes to the ledger. Every word is escaped here. Standard
library only (invariant 9).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import tomllib
from collections.abc import Callable
from datetime import datetime
from functools import cache
from html import escape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from ..talk import Conversation, Waiting, voice_on_for
from ..talk import reply as talk_reply
from ..talk import tapped as talk_tapped
from ..talk import words as tims_words
from .receipts import ReceiptDesk, app_manifest, filer_name, person_zone, shell
from .viewer import ViewerError

TALK_PATH = "/talk"
SAY_PATH = "/talk/say"
PHOTO_PATH = "/talk/photo"
YES_PATH = "/talk/yes"
MANIFEST_PATH = "/talk/app.webmanifest"
HOME_QUERY = "home"
"""Where Tim's own voice is on, the home screen opens ``/talk?home`` (its
manifest's start), which tells setup his page is there (card T2)."""
PATHS = frozenset({TALK_PATH, SAY_PATH, PHOTO_PATH, YES_PATH, MANIFEST_PATH})

MAX_SAID = 2000
"""Characters in one message: a long voice note, short of a pasted file."""
MAX_FORM = 12 * MAX_SAID + 64
"""Bytes a message's form may take once encoded (a character can take 12)."""
SHOWN = 6
"""Exchanges the page shows; the rest stay in her files for Tim."""
REPEAT_SECONDS = 20
"""Her last words again, inside this many seconds of Tim finishing his
answer to them, are the same send twice (a second tap while he worked): she
is shown the answer he gave, and the model isn't asked again."""
RECEIPT_PAGE = "/receipt"

WORDS_FILE = Path(__file__).with_name("talk_page.toml")
_COOKIES = ("__Host-dk_session", "dk_session")
"""The doorkeeper's remembered browser: ``__Host-`` on https, the plain name
on loopback (doorkeeper/server.py ``session_cookie``)."""


class SessionDesk(Protocol):
    def session_person(self, token: str) -> str | None:
        """Whose remembered browser this cookie is, or None."""
        ...


@cache
def words() -> dict[str, str]:
    """Every line a person reads on the page."""
    return dict(tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))["words"])


def csp(nonce: str, *, script: bool = False) -> str:
    """Each page runs at most its own one script, under the nonce: her page's,
    where Tim's voice is on, shows him working after Send; the sign-in's
    talks only to this box. A form posts only here."""
    run = f"'nonce-{nonce}'" if script else "'none'"
    return (
        f"default-src 'none'; script-src {run}; style-src 'nonce-{nonce}'; connect-src 'self'; "
        "manifest-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


def cookie_name(origin: str) -> str:
    """The one cookie that is the box's own: only the ``__Host-`` one on
    https, which a sibling site can never set."""
    return _COOKIES[0] if origin.startswith("https://") else _COOKIES[1]


def _cookie(headers: Any, wanted: str) -> str:
    for part in str(headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == wanted:
            return value.strip()
    return ""


_MORE = (
    ".next{border-left:3px solid var(--accent);padding:.1rem .8rem;margin:0 0 1rem}"
    ".next p{margin:.2rem 0}"
    ".said{margin:.6rem 0;padding:.55rem .8rem;border-radius:12px;white-space:pre-line;"
    "overflow-wrap:anywhere}"
    ".said b{display:block;font-size:.8rem;color:var(--text-2);font-weight:600}"
    ".me{background:var(--bg);margin-left:2rem}"
    ".tim{border:1px solid var(--rule);margin-right:2rem}"
    "textarea{display:block;width:100%;box-sizing:border-box;font:inherit;margin-top:1rem;"
    "padding:.6rem;border:1px solid var(--rule);border-radius:10px;background:var(--bg);"
    "color:var(--text);resize:vertical}"
    "button.quiet{background:none;color:var(--text);border:1px solid var(--rule);"
    "font-weight:400}"
    "details{margin-top:1.2rem}summary{cursor:pointer;color:var(--text-2)}a{color:inherit}"
    ".yes{border:2px solid var(--accent);border-radius:12px;padding:.6rem .8rem;"
    "margin:.6rem 2rem .6rem 0}"
    ".yes p{margin:.2rem 0 .6rem}.yes form{display:flex;gap:.6rem;flex-wrap:wrap}"
    ".photos{display:flex;gap:.6rem;flex-wrap:wrap}.photos form{flex:1 1 10rem}"
    "a.go{display:inline-block;padding:.6rem 1rem;border-radius:10px;background:var(--accent);"
    "color:var(--bg);font-weight:600;text-decoration:none}"
)
_WORKING = (
    ".working{color:var(--text-2);animation:working 1.4s ease-in-out infinite}"
    "@keyframes working{50%{opacity:.45}}"
    "@media (prefers-reduced-motion:reduce){.working{animation:none}}"
    "button:disabled{opacity:.6}"
)
"""Tim's working bubble, on a page where his voice is on (where it's off,
his plain line comes back at once, and the page stays as pinned:
tests/unit/test_voice_off_pinned.py)."""

_HEAD = (
    f'<link rel="manifest" href="{MANIFEST_PATH}">'
    '<link rel="apple-touch-icon" href="/receipt/icon-180.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="Tim">'
)

# The doorkeeper's "Finish setting up" step, from this page (its own pages'
# script, doorkeeper/pages.py _JS_SETUP): options are fetched as the page
# opens, so the tap goes straight to Face ID (iOS 16 to 17.3 open it only
# straight from a tap). Success leaves the browser remembered; the page
# reloads as her.
_SIGN_IN = """
const W=%s;
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b)))
.replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
const unb64u=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
const say=t=>{document.getElementById('status').textContent=t;};
async function post(url,data){let r;try{r=await fetch(url,{method:'POST',credentials:'same-origin',
headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(data)});}
catch(e){return [false,{}];}let d={};try{d=await r.json();}catch(e){}return [r.ok,d];}
let start=null;
async function ready(quiet){const [ok,o]=await post('/setup/start/options',{});
if(!ok){if(!quiet)say(o.message||W.sign_in_trouble);return;}start=o;start.publicKey.challenge=unb64u(start.publicKey.challenge);}
document.getElementById('go').onclick=async()=>{
if(!start){await ready();if(start)say(W.sign_in_ready);return;}
const s=start;start=null;let c;try{c=await navigator.credentials.get({publicKey:s.publicKey});}
catch(e){say(W.sign_in_again);ready();return;}
const r=c.response;const [ok,d]=await post('/setup/start/finish',{attempt:s.attempt,id:c.id,
clientDataJSON:b64u(r.clientDataJSON),authenticatorData:b64u(r.authenticatorData),signature:b64u(r.signature)});
if(!ok){say(d.message||W.sign_in_trouble);ready();return;}location.replace('%s');};
ready(true);
"""


# Her page's one script: the moment she taps Send, her words show as sent
# and Tim's bubble says he's working, and Send can't go twice. The form
# still posts on its own; the box's 303 brings back the page with his reply.
_SENDING = """
const W=%s;
const f=document.getElementById('now');let busy=false;
const line=(cls,who,text)=>{const p=document.createElement('p');p.className=cls;
const b=document.createElement('b');b.textContent=who;p.append(b,text);return p;};
f.addEventListener('submit',e=>{
const box=f.querySelector('textarea');const said=box.value.trim();
if(busy){e.preventDefault();return;}if(!said)return;
busy=true;box.readOnly=true;const go=f.querySelector('button');
const tim=line('said tim working',W.tim,W.working);tim.setAttribute('role','status');
f.before(line('said me',W.you,said),tim);
setTimeout(()=>{go.disabled=true;go.textContent=W.sending;tim.scrollIntoView({block:'nearest'});},0);
});
"""


def _sending_script() -> str:
    """Her page's script, with its words as a value nothing in them can close."""
    keep = ("you", "tim", "working", "sending")
    return _SENDING % json.dumps({k: words()[k] for k in keep}).replace("<", "\\u003c")


def _script_words() -> str:
    """The sign-in's own lines, as a script value nothing in them can close."""
    keep = ("sign_in_ready", "sign_in_again", "sign_in_trouble")
    return json.dumps({k: words()[k] for k in keep}).replace("<", "\\u003c")


def sign_in_page(nonce: str, *, ministry: str = "", back: str = TALK_PATH) -> str:
    """One sign-in for a browser the box doesn't remember yet; it goes on to
    ``back`` once signed in."""
    w = words()
    body = (
        f"<h1>{escape(w['sign_in_title'])}</h1><p>{escape(w['sign_in'])}</p>"
        f'<button id="go" type=button>{escape(w["sign_in_button"])}</button>'
        '<p id="status" role=status></p>'
        f"<p class=note>{escape(w['no_face_id'])} "
        f'<a href="/welcome">{escape(w["new_link"])}</a></p>'
    )
    script = _SIGN_IN % (_script_words(), back)
    return shell(w["title"], body, nonce, ministry=ministry, more=_MORE, head=_HEAD, script=script)


def tapped_line(entry: dict, zone: str) -> str:
    """Her tap on a button, as the one dated line that stays in the thread:
    what the button named, then her answer and when, on her own clock
    ("Sent to Eli. You said yes Thursday, November 19, 3:14 PM.")."""
    w = words()
    tap = entry.get("tap") or {}
    when = datetime.fromisoformat(str(entry["at"])).astimezone(ZoneInfo(zone))
    hour = when.strftime("%I:%M %p").lstrip("0")
    said = w["tapped_yes"] if tap.get("answer") == "yes" else w["tapped_no"]
    what = str(tap.get("what", "")).rstrip(".")
    return w["tapped_line"].format(what=what, said=said, when=f"{when:%A, %B} {when.day}, {hour}")


def render(
    exchanges: list[dict],
    *,
    nonce: str,
    zone: str = "UTC",
    ministry: str = "",
    nxt: dict | None = None,
    photo: bool = False,
    note: str = "",
    waiting: dict | None = None,
    working: bool = False,
) -> str:
    """Her page: the next thing, the last few exchanges, what waits on her
    tap, the box, Send, and the photo button for someone who files
    receipts. ``working``: Tim's voice is on, so Send shows him working and
    can't go twice (its one script). Every word escaped."""
    w = words()
    body: list[str] = []
    if nxt and nxt.get("say"):
        takes = f"<p class=note>{escape(str(nxt['takes']))}</p>" if nxt.get("takes") else ""
        body.append(
            f'<section class=next aria-label="{escape(w["next"])}">'
            f"<p class=note>{escape(w['next'])}</p><p><b>{escape(str(nxt['say']))}</b></p>"
            f"{takes}</section>"
        )
    if not exchanges:
        body.append(f"<p class=note>{escape(w['start'])}</p>")
    for entry in exchanges[-SHOWN:]:
        # her tap on a button stays as one dated line (t-hark-patterns, 2026-11-20)
        you, tim = escape(str(entry.get("said", ""))), escape(str(entry.get("tim", "")))
        mine = (
            f'<p class="said me tap">{escape(tapped_line(entry, zone))}</p>'
            if entry.get("tap")
            else f'<p class="said me"><b>{escape(w["you"])}</b>{you}</p>'
        )
        body.append(mine + f'<p class="said tim"><b>{escape(w["tim"])}</b>{tim}</p>')
    if waiting:  # what waits on her own tap, under Tim's message (card V2)
        body.append(_waiting(waiting, w))
    if note:
        body.append(f"<p role=status class=note><b>{escape(note)}</b></p>")
    body.append(
        f'<form id="now" method=post action="{SAY_PATH}">'
        f'<textarea name="said" rows=3 maxlength={MAX_SAID} required '
        f'aria-label="{escape(w["box"])}" placeholder="{escape(w["box"])}"></textarea>'
        f"<button>{escape(w['send'])}</button></form>"
    )
    if photo:
        # One photo opens the camera; several (a week's pile, or dozens)
        # opens the pile page, where Tim lists what he read for one yes (the
        # owner's read of 2026-11-19).
        body.append(
            f'<div class=photos><form method=post action="{PHOTO_PATH}">'
            f"<button class=quiet>{escape(w['photo'])}</button></form>"
            f'<form method=post action="{PHOTO_PATH}"><input type=hidden name=many value=1>'
            f"<button class=quiet>{escape(w['photos'])}</button></form></div>"
        )
    body.append(
        f"<details><summary>{escape(w['home_screen'])}</summary>"
        f"<p class=note>{escape(w['home_screen_how'])}</p></details>"
    )
    return shell(
        w["title"],
        "".join(body),
        nonce,
        ministry=ministry,
        more=_MORE + _WORKING if working else _MORE,
        head=_HEAD,
        script=_sending_script() if working else "",
    )


def _waiting(waiting: dict, w: dict[str, str]) -> str:
    """A held call as Yes and Not now, with the line code wrote for it; the
    button to her own yes page; or a link a tool made for her, such as a
    receipt's camera (each a link only the box made)."""
    if waiting.get("kind") == "link":
        return (
            f'<section class=yes aria-label="{escape(w["link_title"])}">'
            f'<a class=go href="{escape(str(waiting["link"]))}">'
            f"{escape(str(waiting.get('text', '')))}</a></section>"
        )
    if waiting.get("kind") == "yes_page":
        return (
            f'<section class=yes aria-label="{escape(w["yes_title"])}">'
            f"<p>{escape(w['yes_page'])}</p>"
            f'<a class=go href="{escape(str(waiting["link"]))}">{escape(w["yes_button"])}</a>'
            "</section>"
        )
    return (
        f'<section class=yes aria-label="{escape(w["yes_title"])}">'
        f'<form method=post action="{YES_PATH}">'
        f'<input type=hidden name=id value="{escape(str(waiting.get("id", "")))}">'
        # the button names the act and the amount (t-hark-patterns, 2026-11-20)
        f"<button name=answer value=yes>{escape(str(waiting.get('summary') or w['yes']))}</button>"
        f"<button name=answer value=no class=quiet>{escape(w['not_now'])}</button>"
        "</form></section>"
    )


class TalkPage:
    """The talk page on a box whose doorkeeper remembers browsers."""

    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        ledger_root: str | Path,
        desk: SessionDesk,
        origin: str,
        tools_for: Callable[[str], Any],
        receipts: ReceiptDesk | None = None,
        ministry: str = "",
        adapter: Any = None,
        yes_origin: str = "",
        in_setup: Callable[[str], bool] | None = None,
        on_talked: Callable[[str], Any] | None = None,
        on_home: Callable[[str], Any] | None = None,
    ) -> None:
        self.tenant = tenant
        # Where Tim's own voice is on (card T2), setup hears that she talked
        # to him here (its "where do you want to talk to me" step), and that
        # his page opened from her home screen.
        self.on_talked, self.on_home = on_talked, on_home
        self.in_setup = in_setup  # whose setup still has a step ahead (card V3)
        self.yes_origin = yes_origin.rstrip("/")
        self.tenants_root = Path(tenants_root) if tenants_root else None
        self.ledger_root = ledger_root
        self.desk, self.origin, self.tools_for = desk, origin, tools_for
        self.receipts, self.ministry, self.adapter = receipts, ministry, adapter
        self.cookie = cookie_name(origin)
        # One turn at a time per person, and what each last sent and when Tim
        # finished it: a second tap while he worked waits for his answer and
        # is shown it (REPEAT_SECONDS).
        self._guard = threading.Lock()
        self._turns: dict[str, threading.Lock] = {}
        self._last: dict[str, tuple[str, float]] = {}

    # -- who is asking

    def _person(self, headers: Any) -> tuple[str, Any] | None:
        """(person, their tools) for a browser the doorkeeper remembers as
        someone still in the ministry; None for anyone else."""
        token = _cookie(headers, self.cookie)
        person = self.desk.session_person(token) if token else None
        if not person:
            return None
        try:
            return person, self.tools_for(person)
        except ViewerError:
            return None

    def _files_receipts(self, person: str) -> str:
        if self.receipts is None:
            return ""
        return filer_name(self.tenant, self.tenants_root, person)

    # -- the routes

    def answer(
        self, method: str, path: str, headers: Any, raw: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """One request to a talk path: (status, headers, body)."""
        where, _, query = path.partition("?")
        if where == MANIFEST_PATH and method == "GET":
            name = f"Tim{f' for {self.ministry}' if self.ministry else ''}"
            start = f"{TALK_PATH}?{HOME_QUERY}" if self._voice() else TALK_PATH
            entry = {**app_manifest(start, name, "Tim"), "scope": TALK_PATH}
            data = json.dumps(entry).encode("utf-8")
            return 200, {"Content-Type": "application/manifest+json", **_plain(data)}, data
        if where == TALK_PATH and method == "GET":
            notes = {"long": "too_long", "gone": "gone"}
            home = query == HOME_QUERY and self._voice()
            return self._page(
                headers, note=words()[notes[query]] if query in notes else "", home=home
            )
        if method != "POST" or where not in (SAY_PATH, PHOTO_PATH, YES_PATH):
            return 405, {"Allow": "GET" if where in (TALK_PATH, MANIFEST_PATH) else "POST"}, b""
        given = headers.get("Origin")
        if given and given != self.origin:
            return self._refused()
        found = self._person(headers)
        if found is None:
            return _back(TALK_PATH)
        person, tools = found
        if where == PHOTO_PATH:
            name = self._files_receipts(person)
            if not name or self.receipts is None:
                return _back(TALK_PATH)
            many = parse_qs(raw.decode("utf-8", "replace")).get("many", [""])[0] == "1"
            if many:  # several at once: the pile page, good for the day
                token = self.receipts.issue_pile(person, name, by=person)
                return _back(f"{RECEIPT_PAGE}?many#{token}")
            return _back(f"{RECEIPT_PAGE}#{self.receipts.issue(person, name, None)}")
        if len(raw) > MAX_FORM:
            return _back(TALK_PATH + "?long#now")
        if where == YES_PATH:
            with self._turn(person):
                return self._tap(person, tools, parse_qs(raw.decode("utf-8", "replace")))
        said = parse_qs(raw.decode("utf-8", "replace")).get("said", [""])[0].strip()
        if not said:
            return _back(TALK_PATH + "#now")
        if len(said) > MAX_SAID:
            return _back(TALK_PATH + "?long#now")
        with self._turn(person):
            if self._again(person, said):
                return _back(TALK_PATH + "#now")
            self._say(person, tools, said)
            self._last[person] = (said, time.monotonic())
        if self.on_talked is not None and self._voice():
            self.on_talked(person)
        return _back(TALK_PATH + "#now")

    def _voice(self) -> bool:
        return voice_on_for(self.tenant, self.tenants_root)

    def _turn(self, person: str) -> threading.Lock:
        """Her own turn: her messages are answered one at a time, in order."""
        with self._guard:
            return self._turns.setdefault(person, threading.Lock())

    def _again(self, person: str, said: str) -> bool:
        """Her last words again, sent before Tim's answer to them was a few
        seconds old: the same send twice."""
        last = self._last.get(person)
        return last is not None and last[0] == said and time.monotonic() - last[1] <= REPEAT_SECONDS

    def _setup(self, person: str) -> Callable[[], bool] | None:
        """Asked only when it could step Tim up: whether her setup has a step
        ahead."""
        if self.in_setup is None:
            return None
        ask = self.in_setup
        return lambda: ask(person)

    def _say(self, person: str, tools: Any, said: str) -> None:
        try:
            talk_reply(
                tools,
                said,
                tenant=self.tenant,
                ledger_root=self.ledger_root,
                tenants_root=self.tenants_root,
                adapter=self.adapter,
                in_setup=self._setup(person),
            )
        except Exception:  # noqa: BLE001  her words are kept, and Tim says he lost the thread
            talk = Conversation(self.ledger_root, self.tenant, person)
            talk.add(said, tims_words()["trouble"], [], status="trouble")

    def _tap(self, person: str, tools: Any, form: dict) -> tuple[int, dict[str, str], bytes]:
        """Her Yes or Not now: the held call runs as her, once, exactly as it
        was held (core.talk.tapped). A button that's gone says so."""
        given = form.get("id", [""])[0]
        yes = form.get("answer", [""])[0] == "yes"
        try:
            answer = talk_tapped(
                tools,
                given,
                yes=yes,
                tenant=self.tenant,
                ledger_root=self.ledger_root,
                tenants_root=self.tenants_root,
                adapter=self.adapter,
                in_setup=self._setup(person),
            )
        except Exception:  # noqa: BLE001  her tap is kept, and Tim says he lost the thread
            talk = Conversation(self.ledger_root, self.tenant, person)
            talk.add(
                tims_words()["tapped_yes"], tims_words()["trouble_after"], [], status="trouble"
            )
            return _back(TALK_PATH + "#now")
        return _back(TALK_PATH + ("#now" if answer is not None else "?gone#now"))

    def _shown(self, person: str, tools: Any) -> dict | None:
        """What waits on her tap, as her page may show it: a link only on
        this box's own origin, made relative. A yes page whose cards are all
        decided is gone (after her Face ID it stood into the next
        conversation; the persona year on his voice, 2026-11-18)."""
        waiting = Waiting(self.ledger_root, self.tenant, person)
        entry = waiting.shown()
        if entry is None or entry.get("kind") not in ("yes_page", "link"):
            return entry
        link = str(entry.get("link", ""))
        if entry.get("kind") == "link":
            if link.startswith(self.origin + "/"):
                return {**entry, "link": link[len(self.origin) :]}
            return None
        if entry.get("cards") and not _still_waiting(tools, entry["cards"]):
            waiting.drop()
            return None
        if link.startswith(self.origin + "/"):
            return {**entry, "link": link[len(self.origin) :]}
        if self.yes_origin and link.startswith(self.yes_origin + "/"):
            return entry  # the doorkeeper on its own origin (a box with no proxy in front)
        return None

    def _page(
        self, headers: Any, *, note: str = "", home: bool = False
    ) -> tuple[int, dict[str, str], bytes]:
        nonce = secrets.token_urlsafe(16)
        found = self._person(headers)
        if found is None:
            back = f"{TALK_PATH}?{HOME_QUERY}" if home else TALK_PATH
            html, script = sign_in_page(nonce, ministry=self.ministry, back=back), True
        else:
            person, tools = found
            if home and self.on_home is not None:
                self.on_home(person)
            working = self._voice()
            html, script = (
                render(
                    Conversation(self.ledger_root, self.tenant, person).recent(),
                    nonce=nonce,
                    zone=person_zone(self.tenant, self.tenants_root, person),
                    ministry=self.ministry,
                    nxt=_next(tools),
                    photo=bool(self._files_receipts(person)),
                    note=note,
                    waiting=self._shown(person, tools),
                    working=working,
                ),
                working,
            )
        data = html.encode("utf-8")
        out = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": csp(nonce, script=script),
            # same-origin: her form carries this box's Origin, never "null"
            "Referrer-Policy": "same-origin",
            **_plain(data),
        }
        return 200, out, data

    def _refused(self) -> tuple[int, dict[str, str], bytes]:
        nonce = secrets.token_urlsafe(16)
        w = words()
        body = f"<h1>{escape(w['title'])}</h1><p>{escape(w['not_from_here'])}</p>"
        data = shell(w["title"], body, nonce, ministry=self.ministry, more=_MORE).encode("utf-8")
        out = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": csp(nonce),
            "Referrer-Policy": "same-origin",
            **_plain(data),
        }
        return 403, out, data


def _still_waiting(tools: Any, cards: list) -> bool:
    """Whether any of these cards still waits on a yes; a broken lookup
    keeps the button (her yes page says when a card is already decided)."""
    try:
        rows = tools.call("waiting_cards", {"limit": 200})["rows"]
    except Exception:  # noqa: BLE001
        return True
    pending = {str(row.get("card")) for row in rows}
    return any(str(card) in pending for card in cards)


def _next(tools: Any) -> dict | None:
    """The one thing waiting on her; a broken one never costs her the page."""
    from .next_thing import of

    try:
        return of(tools)
    except Exception:  # noqa: BLE001
        return None


def _plain(data: bytes) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(len(data)),
    }


def _back(location: str) -> tuple[int, dict[str, str], bytes]:
    """303: the browser asks for ``location`` with a GET, so a reload never
    posts again."""
    return 303, {"Location": location, "Cache-Control": "no-store", "Content-Length": "0"}, b""


def page_for(
    desk: Any,
    *,
    tenant: str,
    tenants_root: str | Path | None,
    ledger_root: str | Path,
    origin: str,
    tools_for: Callable[[str], Any],
    receipts: ReceiptDesk | None,
    ministry: str,
    yes_origin: str = "",
    in_setup: Callable[[str], bool] | None = None,
    on_talked: Callable[[str], Any] | None = None,
    on_home: Callable[[str], Any] | None = None,
) -> TalkPage | None:
    """The talk page, on a box whose doorkeeper remembers browsers."""
    if desk is None:
        return None
    return TalkPage(
        tenant,
        tenants_root=tenants_root,
        ledger_root=ledger_root,
        desk=desk,
        origin=origin,
        tools_for=tools_for,
        receipts=receipts,
        ministry=ministry,
        yes_origin=yes_origin,
        in_setup=in_setup,
        on_talked=on_talked,
        on_home=on_home,
    )


def is_talk(path: str) -> bool:
    return urlsplit(path).path in PATHS


__all__ = [
    "HOME_QUERY",
    "MANIFEST_PATH",
    "MAX_FORM",
    "MAX_SAID",
    "PATHS",
    "PHOTO_PATH",
    "REPEAT_SECONDS",
    "SAY_PATH",
    "SHOWN",
    "TALK_PATH",
    "YES_PATH",
    "TalkPage",
    "cookie_name",
    "csp",
    "is_talk",
    "page_for",
    "render",
    "sign_in_page",
    "words",
]
