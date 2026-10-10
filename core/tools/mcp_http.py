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
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

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
        request = urllib.request.Request(  # noqa: S310  scheme checked in __init__
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
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
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
) -> ThreadingHTTPServer:
    """An HTTP server for ``tenant``'s read tools. Bind it to localhost and put
    a TLS proxy in front; ``resource`` is the public URL of the endpoint.

    With ``desk`` (core/tools/witness_desk.WitnessDesk, the box's
    doorkeeper), a tenant whose authority.toml opened the door also gets
    ``decide_card``: a person decides a card with their own Face ID
    (core/tools/decide.py)."""
    from .decide import AskBook, Decider

    asks = AskBook()
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
        return Tools(
            tenant,
            ledger_root=ledger_root,
            obligations_file=obligations_file,
            today=today(),
            viewer=viewer,
            **kwargs,
        )

    door = _Door(verifier, tools_for, origin, prm_url)

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

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in (PRM_PATH, PRM_PATH + ENDPOINT):
                self._reply(200, prm)
            elif path == ENDPOINT:
                self._reply(405, None, {"Allow": "POST"})
            else:
                self._reply(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            self._reply(405, None, {"Allow": "POST"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != ENDPOINT:
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
            self._reply(*door.answer(self.headers, body, too_large=length > MAX_BODY))

    return ThreadingHTTPServer((host, port), Handler)


class _Door:
    """One POST to the endpoint, from headers and body to (status, body,
    headers): origin, then sign-in, then the message, then the person's tools."""

    def __init__(
        self, verifier: Verifier, tools_for: Callable[[str], Tools], origin: str, prm_url: str
    ):
        self.verifier = verifier
        self.tools_for = tools_for
        self.origin = origin
        self.prm_url = prm_url

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
        offered = {s.name for s in tools.specs()}
        said = DOOR_INSTRUCTIONS if "decide_card" in offered else INSTRUCTIONS
        return 200, McpServer(tools, instructions=said).handle(msg), {}


DOOR_INSTRUCTIONS = (
    "Tools over this tenant's back-office ledger, answered as the signed-in person. Every "
    "money value is an exact decimal string; quote it as given and cite its source. One tool "
    "writes: decide_card. It decides nothing by itself: it returns a link, the person opens "
    "it on their phone and taps the button with Face ID or a fingerprint, and only then does "
    "a second call decide the card. Never call decide_card unless the person asked for that "
    "decision in their own words."
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
