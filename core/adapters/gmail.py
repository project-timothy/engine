"""Gmail adapter: the mail seam's five methods over the Gmail REST API.

The same shape as the Graph adapter, with Google OAuth in place of the
Microsoft device-code flow. Reads freely, never moves, marks, or deletes
mail; the single write is ``send_mail``, whose only caller sits behind an
approved card (invariant 7). Standard library only: ``urllib`` for the
transport, ``email`` for the MIME the send builds, no new dependency
(invariant 9).

Auth. Google's device flow does not serve the Gmail scopes, so the owner's
one-time consent is the installed-app loopback flow (``loopback_consent``,
behind ``engine mail consent <tenant>`` when ``[mail].provider = "gmail"``):
a URL to open, a code caught on 127.0.0.1, exchanged for a refresh token
that is stored in the OS keychain under the tenant's names. The jobs'
provider (``refresh_token_provider``) trades that refresh token for an access
token on every run. The client secret an installed-app client needs for the
exchange is named by ``[mail].client_secret_env`` and read from the
environment, never from the repo. Tests inject ``transport``, the token
provider, and the consent's code source, and touch neither keyring nor a
socket.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from ..contracts.mail import MailAttachmentRef, MailAuthError, MailError, MailSummary

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

Transport = Callable[..., Any]  # returns parsed JSON (dict) or raw bytes
TokenProvider = Callable[[], str]


class GmailError(MailError):
    pass


class GmailAuthError(GmailError, MailAuthError):
    """No usable refresh token, or Google refused to trade it."""


def _default_transport(url: str, *, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return json.loads(body) if "application/json" in ctype else body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise GmailError(f"HTTP {exc.code} from {url.split('?')[0]}: {detail}") from exc


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _form(url: str, fields: dict, transport: Transport) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    result = transport(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    return result if isinstance(result, dict) else {}


# ---- auth ---------------------------------------------------------------------


def refresh_token_provider(
    *,
    client_id: str,
    client_secret: str,
    keychain_service: str,
    keychain_account: str,
    transport: Transport | None = None,
) -> TokenProvider:
    """Trade the keychain's refresh token for an access token. The refresh
    token is the durable credential; Google keeps it valid until the owner
    revokes it or leaves it unused for six months."""

    def provider() -> str:
        import keyring  # lazy: tests never import these

        stored = keyring.get_password(keychain_service, keychain_account)
        if not stored:
            raise GmailAuthError(
                f"no Gmail refresh token in keychain ({keychain_service}); run "
                "`engine mail consent <tenant>` (the owner's one-time loopback consent) first"
            )
        try:
            refresh = str(json.loads(stored).get("refresh_token", ""))
        except (TypeError, ValueError):
            refresh = ""
        if not refresh:
            raise GmailAuthError(
                f"keychain entry {keychain_service} holds no refresh token; "
                "re-run `engine mail consent <tenant>`"
            )
        result = _form(
            TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            transport or _default_transport,
        )
        if "access_token" not in result:
            detail = str(result.get("error_description") or result.get("error") or "no result")
            raise GmailAuthError(
                f"refresh-token exchange failed: {detail[:200]}; "
                "re-run `engine mail consent <tenant>`"
            )
        return str(result["access_token"])

    return provider


@dataclass(frozen=True)
class ConsentResult:
    """What a completed consent may say about itself. Never a token."""

    username: str
    expires_in: int


def _loopback_code(server: Any) -> str:
    """Serve exactly one request on the bound loopback server and return the
    ``code`` it carried. Google redirects the owner's browser here."""
    from http.server import BaseHTTPRequestHandler

    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server contract
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = (query.get("code") or [""])[0]
            captured["error"] = (query.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Consent received. You can close this tab.\n")

        def log_message(self, *args: Any) -> None:  # silence the access log
            return

    server.RequestHandlerClass = Handler
    server.handle_request()
    if captured.get("error"):
        raise GmailAuthError(f"consent declined: {captured['error']}")
    return captured.get("code", "")


def loopback_consent(
    *,
    client_id: str,
    client_secret: str,
    scopes: list[str],
    keychain_service: str,
    keychain_account: str,
    prompt: Callable[[str], None],
    code_source: Callable[[str], str] | None = None,
    transport: Transport | None = None,
    port: int = 0,
) -> ConsentResult:
    """The owner's interactive consent (an owner act; no job calls this).
    Binds a loopback listener, hands ``prompt`` the authorization URL BEFORE
    blocking, waits for Google to redirect the browser back with a code,
    exchanges it, and stores the refresh token under the tenant's keychain
    names: the same entry ``refresh_token_provider`` reads. Nothing is
    written when the exchange fails.

    ``code_source`` replaces the listener in tests: it receives the URL and
    returns the code.
    """
    send = transport or _default_transport
    if code_source is None:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        server = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)  # _loopback_code swaps it
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        source = lambda _url: _loopback_code(server)  # noqa: E731
    else:
        redirect_uri = f"http://127.0.0.1:{port or 8765}/"
        source = code_source
    auth_url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "access_type": "offline",
                "prompt": "consent",
                "login_hint": keychain_account,
            }
        )
    )
    prompt(auth_url)
    code = source(auth_url)
    if not code:
        raise GmailAuthError("consent did not return an authorization code")
    result = _form(
        TOKEN_URL,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        send,
    )
    if "refresh_token" not in result:
        detail = str(result.get("error_description") or result.get("error") or "no result")
        raise GmailAuthError(
            f"authorization-code exchange returned no refresh token: {detail[:200]}"
        )
    import keyring  # lazy: tests never import these

    keyring.set_password(
        keychain_service, keychain_account, json.dumps({"refresh_token": result["refresh_token"]})
    )
    return ConsentResult(
        username=_email_claim(str(result.get("id_token", ""))),
        expires_in=int(result.get("expires_in", 0)),
    )


def _email_claim(id_token: str) -> str:
    """The ``email`` claim of an id token, read without verification: it is
    printed for the owner's eyes, never trusted for anything."""
    try:
        payload = id_token.split(".")[1]
        return str(json.loads(_b64url_decode(payload)).get("email", ""))
    except (IndexError, ValueError, TypeError):
        return ""


# ---- the client -----------------------------------------------------------------


def _iso_utc(internal_date_ms: str | int) -> str:
    seconds = int(internal_date_ms) / 1000
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header(payload: dict, name: str) -> str:
    for item in payload.get("headers") or []:
        if str(item.get("name", "")).lower() == name.lower():
            return str(item.get("value", ""))
    return ""


def _walk(part: dict):
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


class GmailClient:
    """Read-only mailbox access bound to one token provider, plus the one send."""

    def __init__(
        self, *, token_provider: TokenProvider, transport: Transport | None = None
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport or _default_transport
        self._token: str | None = None

    def _headers(self) -> dict:
        if self._token is None:
            self._token = self._token_provider()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _get(self, url: str):
        return self._transport(url, data=None, headers=self._headers())

    @staticmethod
    def query(*, since: str, with_attachments: bool, folder: str) -> str:
        """Gmail's search string for the listing. ``after:`` takes the date
        with slashes; an empty folder is the whole mailbox (Gmail's search
        leaves spam and trash out on its own)."""
        terms = [f"after:{since.replace('-', '/')}"]
        if with_attachments:
            terms.append("has:attachment")
        name = folder.strip()
        if name:
            terms.append(
                "in:inbox" if name.lower() == "inbox" else f"label:{name.replace(' ', '-')}"
            )
        return " ".join(terms)

    def list_messages(
        self, *, since: str, with_attachments: bool = True, folder: str = "Inbox"
    ) -> list[MailSummary]:
        params = {
            "q": self.query(since=since, with_attachments=with_attachments, folder=folder),
            "maxResults": "100",
        }
        ids: list[str] = []
        token = ""
        while True:
            page_params = {**params, **({"pageToken": token} if token else {})}
            page = self._get(f"{GMAIL_BASE}/messages?{urllib.parse.urlencode(page_params)}")
            ids.extend(str(m["id"]) for m in page.get("messages", []))
            token = str(page.get("nextPageToken", ""))
            if not token:
                break
        summaries: list[MailSummary] = []
        for message_id in ids:
            meta = self._get(
                f"{GMAIL_BASE}/messages/{message_id}?format=metadata"
                "&metadataHeaders=Subject&metadataHeaders=From"
            )
            payload = meta.get("payload") or {}
            summaries.append(
                MailSummary(
                    id=message_id,
                    subject=_header(payload, "Subject"),
                    sender=parseaddr(_header(payload, "From"))[1],
                    received=_iso_utc(meta.get("internalDate", 0)),
                )
            )
        # Gmail lists newest first; the seam promises oldest first.
        summaries.sort(key=lambda s: (s.received, s.id))
        return summaries

    def _full(self, message_id: str) -> dict:
        page = self._get(f"{GMAIL_BASE}/messages/{message_id}?format=full")
        if not isinstance(page, dict):
            raise GmailError("the message endpoint returned no JSON body")
        return page

    def get_body(self, message_id: str) -> tuple[str, str]:
        """``("text" | "html", content)``: the plain part when there is one,
        else the HTML part, else empty."""
        payload = self._full(message_id).get("payload") or {}
        found: dict[str, str] = {}
        for part in _walk(payload):
            if part.get("filename"):
                continue
            data = (part.get("body") or {}).get("data")
            mime = str(part.get("mimeType", ""))
            if data and mime in ("text/plain", "text/html") and mime not in found:
                found[mime] = _b64url_decode(str(data)).decode("utf-8", errors="replace")
        if "text/plain" in found:
            return "text", found["text/plain"]
        if "text/html" in found:
            return "html", found["text/html"]
        return "", ""

    def list_attachments(self, message_id: str) -> list[MailAttachmentRef]:
        payload = self._full(message_id).get("payload") or {}
        refs: list[MailAttachmentRef] = []
        for part in _walk(payload):
            name = str(part.get("filename") or "")
            body = part.get("body") or {}
            if name and body.get("attachmentId"):
                refs.append(
                    MailAttachmentRef(
                        id=str(body["attachmentId"]),
                        name=name,
                        size=int(body.get("size", 0) or 0),
                        content_type=str(part.get("mimeType", "")),
                    )
                )
        return refs

    def download(self, message_id: str, attachment_id: str) -> bytes:
        page = self._get(f"{GMAIL_BASE}/messages/{message_id}/attachments/{attachment_id}")
        if not isinstance(page, dict) or "data" not in page:
            raise GmailError("attachment endpoint returned no data")
        return _b64url_decode(str(page["data"]))

    def send_mail(
        self,
        *,
        subject: str,
        body: str,
        to: Sequence[str],
        attachments: Sequence[tuple[str, str, bytes]] = (),
    ) -> None:
        """The adapter's one write: send from the signed-in mailbox. Gmail
        files a copy in Sent on its own. Callers MUST hold an approved card."""
        if not to:
            raise GmailError("send_mail called with no recipients")
        message = EmailMessage()
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        message.set_content(body)
        for name, content_type, data in attachments:
            maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
            message.add_attachment(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=name,
            )
        payload = json.dumps({"raw": _b64url_encode(message.as_bytes())}).encode()
        headers = {**self._headers(), "Content-Type": "application/json"}
        self._transport(f"{GMAIL_BASE}/messages/send", data=payload, headers=headers)
