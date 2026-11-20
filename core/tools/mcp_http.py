"""The read tools over HTTP, signed in as a person (Tim's front door, step 3).

``engine mcp`` speaks stdio, which a phone cannot launch. This serves the
same tools on one HTTP endpoint for a hosted Tim box, behind a TLS proxy:
the MCP Streamable HTTP binding of the 2025-06-18 and 2025-11-25 revisions,
served statelessly (no session ids, no streams: each POST gets one JSON
reply), with the MCP authorization spec's resource-server half.

- Every POST needs ``Authorization: Bearer <token>``. A verifier turns the
  token into a person in authority.toml, and every answer is that person's
  (``Viewer``, core/tools/viewer.py). Missing or bad: 401 with a
  ``WWW-Authenticate`` challenge naming the Protected Resource Metadata
  (RFC 9728), served publicly at ``PRM_PATH``.
- An ``Origin`` that is not the resource's own is refused (403, DNS
  rebinding). GET and DELETE are 405. A notification is 202.
- A 2026-07-28 request (no ``initialize``; per-request ``_meta``) gets a 400
  whose error is not a recognized modern one, so a dual-era client falls
  back to ``initialize``. Serving the modern revision is a follow-up.

The sign-in service that issues tokens is its own program on the box, never
part of the engine (owner decision 2026-10-09): the doorkeeper,
github.com/project-timothy/doorkeeper. ``Introspection`` asks it who a token
is (RFC 7662). ``RefuseAll`` admits nobody, and ``TokenFile`` admits holders
of invitation tokens whose SHA-256 hashes sit in a file outside the
repository, one person each. Standard library only (invariant 9).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from ..engine import web
from ..engine.config import load_tenant, tenant_dir
from ..mileage.door import TrackDoor
from ..mileage.setup_link import TRACK_PATH
from ..onboarding.interview import INTERVIEWS, InterviewBook
from ..onboarding.person import SetupBook
from ..talk import voice_on_for
from .catalog import Tools
from .mcp_stdio import INSTRUCTIONS, SUPPORTED_VERSIONS, McpServer
from .viewer import ViewerError, viewer_for

ENDPOINT = "/mcp"
PRM_PATH = "/.well-known/oauth-protected-resource"
MAX_BODY = 1_000_000


class Verifier(Protocol):
    def person(self, token: str) -> str | None:
        """The person (an id in authority.toml) this token signs in, or None."""
        ...


class RefuseAll:
    """The default: no sign-in service is configured, so nobody is admitted."""

    def person(self, token: str) -> str | None:
        return None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenFile:
    """Invitation tokens: a JSON file of ``{sha256(token): person}``, read on
    every lookup so a new invitation works without a restart. Only hashes are
    kept; the token itself is shown once, when it is made."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def person(self, token: str) -> str | None:
        if not token or not self.path.is_file():
            return None
        held = json.loads(self.path.read_text(encoding="utf-8"))
        wanted = _digest(token)
        for digest, person in held.items():
            if hmac.compare_digest(digest, wanted):
                return str(person)
        return None


LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def service_url(url: str) -> str:
    """A sign-in service's address: https, or http on loopback, where the
    doorkeeper sits on a Tim box; never a query or fragment."""
    parts = urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in LOOPBACK
    if not (parts.scheme == "https" or local) or parts.query or parts.fragment:
        raise ValueError(f"{url}: the sign-in service is https, or http on loopback")
    return url


def service_secret(secret_file: str | Path) -> str:
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise ValueError(f"{secret_file} holds no usable secret")
    return secret


class Introspection:
    """Asks the box's sign-in service who a token is (RFC 7662), POSTing the
    token in the body with the box's shared secret as a bearer header.

    Only an active access token whose audience is this endpoint names a
    person; anything else, including a service that cannot be reached or
    answers nonsense, is nobody. The service is reached over https, or over
    http on loopback, where it sits on a Tim box."""

    def __init__(
        self, *, url: str, secret_file: str | Path, resource: str, timeout: float = 5.0
    ) -> None:
        self.url, self.resource, self.timeout = service_url(url), resource, timeout
        self._secret = service_secret(secret_file)

    def person(self, token: str) -> str | None:
        if not token:
            return None
        request = web.request(
            self.url,
            data=urlencode({"token": token}).encode("ascii"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._secret}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with web.urlopen(request, timeout=self.timeout) as resp:
                answer = json.loads(resp.read(65536))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return self._person_in(answer)

    def _person_in(self, answer: Any) -> str | None:
        if not isinstance(answer, dict) or answer.get("active") is not True:
            return None
        if answer.get("token_type", "access_token") != "access_token":
            return None
        aud = answer.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self.resource not in audiences:
            return None
        exp = answer.get("exp")
        if isinstance(exp, int | float) and exp < time.time():
            return None
        sub = answer.get("sub")
        return sub if isinstance(sub, str) and sub else None


def new_token(path: str | Path, person: str) -> str:
    """A fresh invitation token for ``person``; its hash is added to the file
    and the token is returned once, to hand to that person."""
    path = Path(path)
    held = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    token = secrets.token_urlsafe(32)
    held[_digest(token)] = person
    path.write_text(json.dumps(held, indent=1) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


LAST_HANDSHAKE_REVISION = "2025-11-25"
"""The last MCP revision that opens with ``initialize``; later ones are modern."""


def _modern(msg: dict, headers: Any) -> bool:
    """A modern-era request (2026-07-28 on): per-request metadata instead of a
    handshake. ``initialize`` itself always belongs to the handshake era."""
    if msg.get("method") == "initialize":
        return False
    meta = (msg.get("params") or {}).get("_meta") or {}
    version = headers.get("MCP-Protocol-Version", "")
    return "io.modelcontextprotocol/protocolVersion" in meta or version > LAST_HANDSHAKE_REVISION


def make_server(
    tenant: str,
    *,
    tenants_root: str | Path | None,
    ledger_root: Path,
    obligations_file: Path | None,
    verifier: Verifier,
    resource: str,
    authorization_servers: list[str],
    host: str = "127.0.0.1",
    port: int = 8765,
    today: Callable[[], str | None] = lambda: None,
    lead_days: tuple[int, ...] | None = None,
    desk: Any = None,
    receipts: Any = None,
    tracker: Any = None,
    month_key: Path | None = None,
) -> ThreadingHTTPServer:
    """An HTTP server for ``tenant``'s read tools. Bind it to localhost and put
    a TLS proxy in front; ``resource`` is the public URL of the endpoint.

    With ``desk`` (core/tools/witness_desk.WitnessDesk, the box's
    doorkeeper), a tenant whose authority.toml opened the door also gets
    ``decide_card``: a person decides a card with their own Face ID
    (core/tools/decide.py). With ``receipts`` (core/tools/receipts.ReceiptDesk),
    a person who files expenses also gets ``add_receipt`` and the box serves
    the camera page at ``receipts.PAGE_PATH``. With ``desk``, the box also
    shows a reviewer a person's month (core/tools/month_page.py) to a grant
    the doorkeeper's month page posts after their Face ID."""
    from . import month_page as mp
    from . import receipts as rcpt
    from . import setup_page as sp
    from . import talk_page as tk
    from .decide import AskBook, Decider
    from .first_time import Shown

    asks = AskBook()
    ministry = load_tenant(tenant, tenants_root=tenants_root).identity.legal_name
    shown = Shown(tenant_dir(tenant, tenants_root=Path(tenants_root) if tenants_root else None))
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(resource))
    prm_url = origin + PRM_PATH
    prm = {
        "resource": resource,
        "authorization_servers": list(authorization_servers),
        "bearer_methods_supported": ["header"],
    }

    def tools_for(person: str) -> Tools:
        viewer = viewer_for(tenant, person, tenants_root=tenants_root)
        kwargs: dict[str, Any] = {"lead_days": lead_days} if lead_days else {}
        if desk is not None:
            kwargs["decider"] = Decider(
                tenant,
                tenants_root=tenants_root,
                ledger_root=ledger_root,
                person=person,
                desk=desk,
                asks=asks,
            )
        if receipts is not None:
            kwargs["receipts"] = rcpt.Receipts(
                tenant,
                tenants_root=tenants_root,
                ledger_root=ledger_root,
                person=person,
                desk=receipts,
                base_url=origin + rcpt.PAGE_PATH,
            )
        return Tools(
            tenant,
            ledger_root=ledger_root,
            obligations_file=obligations_file,
            today=today(),
            viewer=viewer,
            ministry=ministry,
            shown=shown,
            talk_page=_own_page(desk, tenant, tenants_root, origin),
            **kwargs,
        )

    folder = tenant_dir(tenant, tenants_root=Path(tenants_root) if tenants_root else None)
    book = SetupBook(folder)
    reads = partial(_reads, tenant, tenants_root)

    asked = _Asked(book)
    door = _Door(verifier, tools_for, origin, prm_url, on_asked=asked)
    month = mp.page_for(desk, tools_for=tools_for, ministry=ministry, key_file=month_key)
    # The missionary's own setup questions, from a link a maintainer made on
    # this box (core/onboarding/interview.py, board c49): kept beside its books.
    interviews = InterviewBook(Path(ledger_root).parent / INTERVIEWS)
    setup = sp.page_for(
        desk,
        tenant=tenant,
        tenants_root=tenants_root,
        origin=origin,
        ministry=ministry,
        connector=resource,
        tools_for=tools_for,
    )
    # Tim's own page, where a person talks to him (Standalone Tim, card T1):
    # who is asking is the browser the doorkeeper remembers. Someone still
    # in her own setup gets Tim's strong model (card V3), by setup's word.
    # Where his voice is on (card T2), her first word to him here finishes
    # setup's "where do you want to talk to me" step, as her AI's first
    # question does, and his page opened from her home screen finishes that
    # step.
    talk = tk.page_for(
        desk,
        in_setup=setup.unfinished if setup is not None else None,
        on_talked=asked,
        on_home=lambda person: _seen(book, person, "home_screen"),
        tenant=tenant,
        tenants_root=tenants_root,
        ledger_root=ledger_root,
        origin=origin,
        tools_for=tools_for,
        receipts=receipts,
        ministry=ministry,
        # Her yes page is the doorkeeper's: the box's own origin behind its
        # proxy, or the doorkeeper's own where nothing stands in front.
        yes_origin="{0.scheme}://{0.netloc}".format(urlsplit(authorization_servers[0]))
        if authorization_servers
        else "",
    )
    home = (
        rcpt.HomeLinks(
            tenant,
            tenants_root=tenants_root,
            desk=receipts,
            on_home=lambda person: _seen(book, person, "home_screen"),
        )
        if receipts is not None
        else None
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "timothy-engine"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # no request lines on stderr: a path can carry what a log must not

        def _reply(self, status: int, body: dict | None, headers: dict | None = None) -> None:
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _page(self, html: str, nonce: str) -> None:
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; manifest-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _receipt(self) -> None:
            status, body, close = rcpt.upload(
                receipts,
                self.headers,
                self.rfile.read,
                origin=origin,
                tenant=tenant,
                tenants_root=tenants_root,
                ledger_root=ledger_root,
            )
            self.close_connection = close  # an unread body never reaches the next request
            self._reply(status, body)

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            if receipts is not None and path == rcpt.PAGE_PATH:
                nonce = secrets.token_urlsafe(16)
                many = query == "many"
                page = rcpt.page(
                    nonce,
                    many=many,
                    ministry=ministry,
                    reads=reads(),
                    statements=many and _statements(tenant, tenants_root),
                )
                self._page(page, nonce)
            elif path in (PRM_PATH, PRM_PATH + ENDPOINT):
                self._reply(200, prm)
            elif path == ENDPOINT:
                self._reply(405, None, {"Allow": "POST"})
            else:
                self._reply(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            self._reply(405, None, {"Allow": "POST"})

        def do_POST(self) -> None:  # noqa: N802
            if receipts is not None and self.path.split("?")[0] == rcpt.PAGE_PATH:
                self._receipt()
                return
            if self.path.split("?")[0] != ENDPOINT:
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
            self._reply(*door.answer(self.headers, body, too_large=length > MAX_BODY))

    feeding = _feeding(
        _interviewing(
            _monthly(_talking(Handler, talk), month, origin),
            interviews,
            origin,
            partial(_read_letter, tenant, tenants_root, ledger_root),
        ),
        folder,
        tools_for,
        book,
        ministry,
    )
    tracking = _tracking(
        _setting_up(feeding, home, setup, origin, ministry, reads),
        TrackDoor(
            tenant,
            tenants_root=tenants_root,
            ledger_root=ledger_root,
            tracker=tracker,
            on_heard=lambda person: _seen(book, person, "mileage"),
        ),
    )
    server = ThreadingHTTPServer((host, port), tracking)
    server.flush_drives = tracking.tracks.flush  # type: ignore[attr-defined]  # the minute timer
    return server


def _own_page(desk: Any, tenant: str, tenants_root: Any, origin: str) -> str:
    """Tim's own page, where his voice is on and the box has it (a doorkeeper
    that remembers browsers): start_here names it (card T2)."""
    from .talk_page import TALK_PATH

    return origin + TALK_PATH if desk is not None and voice_on_for(tenant, tenants_root) else ""


def _reads(tenant: str, tenants_root: Any) -> bool:
    """Whether the ministry's file has Tim read a receipt as it lands (card
    R1), read at each page so the switch needs no restart."""
    from .receipt_read import on

    return on(load_tenant(tenant, tenants_root=tenants_root).llm)


def _statements(tenant: str, tenants_root: Any) -> bool:
    """Whether the ministry's file has Tim read a statement's photo or PDF
    sent on a catch-up link (card R2), read at each page."""
    from .statement_read import on

    return on(load_tenant(tenant, tenants_root=tenants_root).llm)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _seen(book: SetupBook, person: str, step: str) -> bool:
    """Setup hears that ``step`` works for ``person``: True once it is done."""
    book.saw(person, step, now=_now())
    return bool(book.answers(person).get(step, {}).get("seen"))


class _Asked:
    """Their first question from their own AI finishes setup's AI step
    (chunk 11): a tool call through the door, as them."""

    def __init__(self, book: SetupBook) -> None:
        self.book = book
        self.done: set[str] = set()

    def __call__(self, person: str) -> None:
        if person not in self.done and _seen(self.book, person, "ai_connector"):
            self.done.add(person)


def _feeding(
    handler: type, folder: Path, tools_for: Callable[[str], Tools], book: SetupBook, ministry: str
) -> type:
    """The door's handler, also answering a person's calendar feed at
    ``/cal/<key>.ics`` (core/tools/calendar_feed.py, chunk 11)."""
    from . import calendar_feed as cal

    class Feeding(handler):  # type: ignore[valid-type, misc]
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if not path.startswith(cal.CAL_PATH):
                super().do_GET()
                return
            person = cal.feed_person(folder, path)
            try:
                tools = tools_for(person) if person else None
            except ViewerError:
                tools = None
            if person is None or tools is None:
                self._reply(404, {"error": "not found"})
                return
            data = cal.render(tools, ministry).encode("utf-8")
            if cal.by_a_calendar(self.headers):
                _seen(book, person, "calendar")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Feeding


def _setting_up(
    handler: type,
    home: Any,
    setup: Any,
    origin: str,
    ministry: str,
    reads: Callable[[], bool] = lambda: False,
) -> type:
    """The door's handler, also serving a person's setup (core/tools/
    setup_page.py) and the receipt page on their home screen (core/tools/
    receipts.py, chunk 10), each only where the box has its part."""
    from . import receipts as rcpt
    from . import setup_page as sp

    class SettingUp(handler):  # type: ignore[valid-type, misc]
        def _routed(self) -> tuple[str, str] | None:
            return rcpt.home_route(self.path.split("?")[0]) if home is not None else None

        def do_GET(self) -> None:  # noqa: N802
            routed = self._routed()
            if routed is None:
                super().do_GET()
                return
            kind, value = routed
            if kind == "page":
                nonce = secrets.token_urlsafe(16)
                self._page(rcpt.page(nonce, ministry=ministry, home=value, reads=reads()), nonce)
                return
            if kind == "manifest":
                data = json.dumps(rcpt.manifest(value, ministry)).encode("utf-8")
                kind, cache = "application/manifest+json", "no-store"
            else:
                data, kind, cache = rcpt.icon(value), "image/png", "max-age=86400"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", cache)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            routed = self._routed()
            limit = 256 if routed else sp.MAX_FORM
            if not (routed and routed[0] == "page") and not (setup and path in sp.PATHS):
                super().do_POST()
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(max(length, 0), limit + 1))
            if setup is not None and path in sp.PATHS:
                self._reply(*setup.answer(path, self.headers, raw))
            elif self.headers.get("Origin") not in (None, origin):
                self._reply(403, {"say": "That didn't come from Tim's page."})
            else:
                standalone = raw.decode("utf-8", "replace") == "standalone=1"
                self._reply(*home.ticket(routed[1], standalone=standalone))

    return SettingUp


def _talking(handler: type, talk: Any) -> type:
    """The door's handler, also serving Tim's own page (core/tools/
    talk_page.py) when the box has a doorkeeper to say who is asking."""
    from . import talk_page as tk

    class Talking(handler):  # type: ignore[valid-type, misc]
        def _talk(self, method: str) -> bool:
            if talk is None or not tk.is_talk(self.path):
                return False
            raw = b""
            if method == "POST":
                length = max(int(self.headers.get("Content-Length") or 0), 0)
                raw = self.rfile.read(min(length, tk.MAX_FORM + 1))
                if length > len(raw):
                    self.close_connection = True  # an unread body never reaches the next request
            status, headers, data = talk.answer(method, self.path, self.headers, raw)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._talk("GET"):
                super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if not self._talk("POST"):
                super().do_POST()

    return Talking


def _monthly(handler: type, month: Any, origin: str) -> type:
    """The door's handler, also showing a reviewer a month at the month
    page's paths when the box has a doorkeeper (core/tools/month_page.py)."""
    from . import month_page as mp

    class Monthly(handler):  # type: ignore[valid-type, misc]
        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] not in mp.PATHS:
                super().do_POST()
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(max(length, 0), mp.MAX_FORM + 1))
            status, headers, data = mp.answer(month, self.path, self.headers, raw, origin)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

    return Monthly


def _read_letter(tenant: str, tenants_root: Any, ledger_root: Any, photo: bytes) -> Any:
    """What an interview's IRS letter reads, where the box's own file
    switches the reader on (card R2), or None; read at each photo so the
    switch needs no restart."""
    from datetime import date

    from . import letter_read
    from .receipts import kind_of

    settings = load_tenant(tenant, tenants_root=tenants_root).llm
    if not letter_read.on(settings):
        return None
    return letter_read.read(
        photo,
        kind_of(photo),
        settings=settings,
        ledger_root=ledger_root,
        tenant=tenant,
        today=date.today(),
    )


def _interviewing(
    handler: type, interviews: Any, origin: str, letter: Callable[[bytes], Any] | None = None
) -> type:
    """The door's handler, also serving a missionary's own setup questions
    at ``/start`` (core/tools/interview_page.py), reading her IRS letter
    with ``letter`` (card R2)."""
    from . import interview_page as ip

    class Interviewing(handler):  # type: ignore[valid-type, misc]
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != ip.START_PATH:
                super().do_GET()
                return
            nonce = secrets.token_urlsafe(16)
            data = ip.start_page(nonce).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", ip.csp(nonce, script=True))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] not in ip.PATHS:
                super().do_POST()
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > ip.MAX_BODY:
                self.close_connection = True  # an unread body never reaches the next request
                self._reply(413, None)
                return
            raw = self.rfile.read(max(length, 0))
            status, headers, data = ip.answer(
                interviews, self.path, self.headers, raw, origin, letter=letter
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

    return Interviewing


def _tracking(handler: type, tracks: TrackDoor) -> type:
    """The door's handler, also taking a phone tracker's positions at
    TRACK_PATH when the box keeps drives (core/mileage)."""

    class Tracking(handler):  # type: ignore[valid-type, misc]
        def _track(self) -> bool:
            if not tracks.on or self.path.split("?")[0] != TRACK_PATH:
                return False
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 4096 else ""
            tracks.take(urlsplit(self.path).query or body)
            self._reply(200, None)
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._track():
                super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if not self._track():
                super().do_POST()

    Tracking.tracks = tracks  # type: ignore[attr-defined]
    return Tracking


class _Door:
    """One POST to the endpoint, from headers and body to (status, body,
    headers): origin, then sign-in, then the message, then the person's tools."""

    def __init__(
        self,
        verifier: Verifier,
        tools_for: Callable[[str], Tools],
        origin: str,
        prm_url: str,
        *,
        on_asked: Callable[[str], None] = lambda person: None,
    ):
        self.verifier = verifier
        self.tools_for = tools_for
        self.origin = origin
        self.prm_url = prm_url
        self.on_asked = on_asked

    def _challenge(self, error: str = "") -> tuple[int, dict, dict]:
        detail = f', error="{error}"' if error else ""
        header = f'Bearer resource_metadata="{self.prm_url}"{detail}'
        return 401, {"error": "sign in to use Tim"}, {"WWW-Authenticate": header}

    def answer(self, headers: Any, body: bytes, *, too_large: bool = False) -> tuple:
        given_origin = headers.get("Origin")
        if given_origin and given_origin != self.origin:
            return 403, {"error": "origin not allowed"}, {}
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._challenge()
        person = self.verifier.person(auth[len("Bearer ") :].strip())
        if person is None:
            return self._challenge("invalid_token")
        if too_large:
            return 413, {"error": "request too large"}, {}
        try:
            msg = json.loads(body or b"null")
        except json.JSONDecodeError as exc:
            return 400, {"jsonrpc": "2.0", "id": None, "error": _err(-32700, str(exc))}, {}
        if not isinstance(msg, dict):
            return 400, {"jsonrpc": "2.0", "id": None, "error": _err(-32600, "")}, {}
        if "id" not in msg or "method" not in msg:
            return 202, None, {}  # a notification (or a stray response): accepted, no reply
        if _modern(msg, headers):
            versions = ", ".join(SUPPORTED_VERSIONS)
            error = _err(-32600, f"this endpoint speaks {versions}; send initialize")
            return 400, {"jsonrpc": "2.0", "id": msg.get("id"), "error": error}, {}
        try:
            tools = self.tools_for(person)
        except ViewerError:
            return 403, {"error": "this sign-in no longer belongs to anyone here"}, {}
        if msg.get("method") == "tools/call":
            self.on_asked(person)
        offered = {s.name for s in tools.specs()}
        said = WHO_TIM + (DOOR_INSTRUCTIONS if "decide_card" in offered else INSTRUCTIONS)
        return 200, McpServer(tools, instructions=said).handle(msg), {}


# Who the person's AI is reaching, so a plain question or any of Tim's names
# comes here (the owner's iPhone walk, 2026-10-29: Claude answered "What should
# I do first?" itself, and "Tim, ..." reached Tim).
WHO_TIM = (
    "These tools are Tim, also called Timothy or Timmy: the signed-in person's own "
    "assistant for their ministry's back office. When they speak to Tim, Timothy or Timmy, "
    "or ask what to do first, what's waiting for them, or about their receipts, expenses, "
    "cash, cards, mileage, budget, the books, reports or deadlines, answer with these tools. "
)

DOOR_INSTRUCTIONS = (
    "Tools over this tenant's back-office ledger, answered as the signed-in person. Every "
    "money value is an exact decimal string; quote it as given. Speak to the person in their "
    "own terms: who, what for, and how much. Card numbers, sources, tool names, and what a "
    "lookup returned stay in your calls, never in your words. Two tools write. add_receipt, "
    'where offered, answers "here\'s a receipt" with a link that opens their camera; the '
    "amount comes from their words, never yours. decide_card decides a card. "
    "At or under the ministry's dollar line, the person's word decides it. Above it, it "
    "returns a link to show the person, and the tap finishes it: when they tap the button "
    "with Face ID or a fingerprint, the card is decided and their page says so, with nothing "
    "more needed from you. Never call decide_card unless the person asked for that decision "
    "in their own words. Every answer carries next, the one thing waiting on the person, "
    "worked out from their books: end your reply with its say in your own words, and when "
    "they agree, call its tool with its args. The first time something new comes up, an "
    "answer also carries first_time: say its what, its why, and its rule (the rule in its "
    "own words) before anything else, then its once line. When they ask why, why says it "
    "again."
)


def _err(code: int, message: str) -> dict:
    return {"code": code, "message": message}


__all__ = [
    "ENDPOINT",
    "PRM_PATH",
    "Introspection",
    "RefuseAll",
    "TokenFile",
    "Verifier",
    "make_server",
    "new_token",
]
