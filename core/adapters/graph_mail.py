"""Microsoft Graph mail adapter: attachment fetch, plus one gated send.

The engine's line into a tenant's mailbox. Reads freely: it lists messages,
lists attachments, downloads bytes. It never moves, marks, or deletes mail —
the mailbox is the owner's. The single write is ``send_mail`` (added
2026-08-03 for the close statements review email), and its only caller sits
behind an approved card in the queue, per invariant 7: every external send
passes through the approval queue, no exceptions.

Auth is injectable. The default provider acquires a token silently from an
MSAL cache stored in the OS keychain (service/account named by tenant
config). That cache is populated by ``device_code_consent`` (the owner act
behind ``engine mail consent <tenant>``, added 2026-09-04 when the retired
pipeline repo that used to perform it went away); the provider and the
consent build the MSAL app and persist the cache through the same two
helpers, so what consent stores is exactly what the jobs read. Tests inject
``token_provider`` and ``transport`` and touch neither msal, keyring, nor
the network. msal/keyring import lazily inside the provider and the consent
only.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.mail import MailAttachmentRef, MailAuthError, MailError, MailSummary

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

Transport = Callable[..., Any]  # returns parsed JSON (dict) or raw bytes
TokenProvider = Callable[[], str]


class GraphMailError(MailError):
    pass


class GraphAuthError(GraphMailError, MailAuthError):
    """No silently-acquirable token: the cached consent is missing/expired."""


def _msal_app(client_id: str, tenant_id: str, cache: Any) -> Any:
    """The one MSAL app construction, shared by the silent provider and the
    consent so the two can never disagree on authority or cache."""
    import msal  # lazy: tests never import these

    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


def _persist_cache(cache: Any, *, keychain_service: str, keychain_account: str) -> None:
    """The one keychain write: the serialized MSAL cache under the tenant's
    names. MSAL rotates refresh tokens, mirroring the QBO adapter's rule:
    never lose a rotated credential."""
    import keyring  # lazy: tests never import these

    keyring.set_password(keychain_service, keychain_account, cache.serialize())


def keychain_token_provider(
    *,
    client_id: str,
    tenant_id: str,
    scopes: list[str],
    keychain_service: str,
    keychain_account: str,
) -> TokenProvider:
    """Silent acquisition from an MSAL cache in the OS keychain. Rewrites the
    cache after acquisition (MSAL rotates refresh tokens), mirroring the
    QBO adapter's rule: never lose a rotated credential."""

    def provider() -> str:
        import keyring  # lazy: tests never import these
        import msal

        cache = msal.SerializableTokenCache()
        stored = keyring.get_password(keychain_service, keychain_account)
        if not stored:
            raise GraphAuthError(
                f"no MSAL token cache in keychain ({keychain_service}); run "
                "`engine mail consent <tenant>` (the owner's one-time device-code consent) first"
            )
        cache.deserialize(stored)
        app = _msal_app(client_id, tenant_id, cache)
        accounts = app.get_accounts()
        if not accounts:
            raise GraphAuthError(
                "MSAL cache holds no account; re-run `engine mail consent <tenant>`"
            )
        result = app.acquire_token_silent(scopes, account=accounts[0])
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "no result")
            raise GraphAuthError(
                f"silent token acquisition failed: {detail[:200]}; "
                "re-run `engine mail consent <tenant>`"
            )
        if cache.has_state_changed:
            _persist_cache(
                cache, keychain_service=keychain_service, keychain_account=keychain_account
            )
        return result["access_token"]

    return provider


@dataclass(frozen=True)
class ConsentResult:
    """What a completed consent may say about itself: who signed in and how
    long the first access token lives. Never a token."""

    username: str
    expires_in: int


def device_code_consent(
    *,
    client_id: str,
    tenant_id: str,
    scopes: list[str],
    keychain_service: str,
    keychain_account: str,
    prompt: Callable[[str, str, int], None],
) -> ConsentResult:
    """The owner's interactive device-code consent (an owner act; no job
    calls this). Starts the flow, hands ``prompt`` the verification URL, the
    user code, and the code's lifetime in seconds BEFORE blocking on the
    sign-in, then persists the serialized cache under the tenant's keychain
    names: the same entry ``keychain_token_provider`` reads. A flow that does
    not start, times out, or is declined raises ``GraphAuthError`` carrying
    MSAL's error and description; nothing is written in that case.

    Persisting is not proof the jobs can use it: the caller verifies by
    building the provider and acquiring silently.
    """
    import msal  # lazy: tests never import these

    cache = msal.SerializableTokenCache()
    app = _msal_app(client_id, tenant_id, cache)
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise GraphAuthError(
            "device-code flow did not start: "
            f"{flow.get('error', 'no error code')}: "
            f"{str(flow.get('error_description', flow))[:300]}"
        )
    prompt(flow["verification_uri"], flow["user_code"], int(flow.get("expires_in", 0)))
    result = app.acquire_token_by_device_flow(flow)  # blocks until sign-in or expiry
    if not result or "access_token" not in result:
        raise GraphAuthError(
            "device-code consent failed: "
            f"{(result or {}).get('error', 'no result')}: "
            f"{str((result or {}).get('error_description', 'no description'))[:300]}"
        )
    # A completed consent always persists: the command's whole purpose is
    # the keychain entry, so this write does not wait on has_state_changed.
    _persist_cache(cache, keychain_service=keychain_service, keychain_account=keychain_account)
    claims = result.get("id_token_claims") or {}
    accounts = app.get_accounts()
    username = claims.get("preferred_username") or (
        accounts[0].get("username", "") if accounts else ""
    )
    return ConsentResult(username=str(username), expires_in=int(result.get("expires_in", 0)))


def _default_transport(url: str, *, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return json.loads(body) if "application/json" in ctype else body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise GraphMailError(f"HTTP {exc.code} from {url.split('?')[0]}: {detail}") from exc


class GraphMailClient:
    """Read-only mailbox access bound to one token provider."""

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

    def list_messages(
        self, *, since: str, with_attachments: bool = True, folder: str = "Inbox"
    ) -> list[MailSummary]:
        """Messages received on/after ``since``, oldest first, across all
        pages, in the seam's shape (``core/contracts/mail.py``).

        ``with_attachments`` keeps only the attachment bearers, which is the
        AP feed's rule and the default; the AR remittance lane passes False,
        because a remittance advice carries no attachment and its numbers are
        in the body (issue #282). Bodies are never listed: a caller reads one
        with :meth:`get_body` after it has matched.

        ``folder`` is the mail folder to read, ``Inbox`` by default. An EMPTY
        folder reads the whole mailbox, every folder included, which is what
        a lane needs when the mail it looks for is routinely filed before the
        lane runs.
        """
        params = urllib.parse.urlencode(
            {
                "$filter": f"receivedDateTime ge {since}T00:00:00Z",
                "$select": "id,subject,receivedDateTime,hasAttachments,from",
                "$orderby": "receivedDateTime asc",
                "$top": "100",
            }
        )
        scope = f"/me/mailFolders/{folder}/messages" if folder.strip() else "/me/messages"
        url: str | None = f"{GRAPH_BASE}{scope}?{params}"
        messages: list[MailSummary] = []
        while url:
            page = self._get(url)
            for m in page.get("value", []):
                if not (m.get("hasAttachments") or not with_attachments):
                    continue
                sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
                messages.append(
                    MailSummary(
                        id=str(m["id"]),
                        subject=str(m.get("subject", "")),
                        sender=str(sender),
                        received=str(m.get("receivedDateTime", "")),
                    )
                )
            url = page.get("@odata.nextLink")
        # The seam promises oldest first; Graph's $orderby is asked for it,
        # and the sort makes the promise the adapter's, not the server's.
        messages.sort(key=lambda s: (s.received, s.id))
        return messages

    def get_body(self, message_id: str) -> tuple[str, str]:
        """``(contentType, content)`` of one message's body.

        A separate call per message on purpose: the listing stays free of
        bodies, so the only bodies this engine ever reads are the ones a
        caller's own filters already matched.
        """
        page = self._get(f"{GRAPH_BASE}/me/messages/{message_id}?$select=body")
        if not isinstance(page, dict):
            raise GraphMailError("the message endpoint returned no JSON body")
        body = page.get("body") or {}
        return str(body.get("contentType", "")), str(body.get("content", ""))

    def list_attachments(self, message_id: str) -> list[MailAttachmentRef]:
        """File attachments only; item/reference attachments are not files."""
        page = self._get(f"{GRAPH_BASE}/me/messages/{message_id}/attachments")
        return [
            MailAttachmentRef(
                id=str(a["id"]),
                name=str(a.get("name", "attachment.bin")),
                size=int(a.get("size", 0) or 0),
                content_type=str(a.get("contentType", "")),
            )
            for a in page.get("value", [])
            if a.get("@odata.type") == "#microsoft.graph.fileAttachment"
        ]

    def download(self, message_id: str, attachment_id: str) -> bytes:
        raw = self._get(f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}/$value")
        if not isinstance(raw, bytes | bytearray):
            raise GraphMailError("attachment $value endpoint returned non-bytes")
        return bytes(raw)

    def send_mail(
        self,
        *,
        subject: str,
        body: str,
        to: Sequence[str],
        attachments: Sequence[tuple[str, str, bytes]] = (),
    ) -> None:
        """The adapter's one write: send from the signed-in mailbox.

        ``attachments`` is (filename, content_type, data) triples, inlined
        as base64 (fine for report-sized files; Graph caps inline at ~3MB).
        Callers MUST hold an approved card — this method trusts the queue,
        it does not re-check it. A copy lands in the owner's Sent Items so
        the send is visible where mail is already reviewed.
        """
        if not to:
            raise GraphMailError("send_mail called with no recipients")
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": content_type,
                    "contentBytes": base64.b64encode(data).decode("ascii"),
                }
                for name, content_type, data in attachments
            ],
        }
        payload = json.dumps({"message": message, "saveToSentItems": True}).encode()
        headers = {**self._headers(), "Content-Type": "application/json"}
        # Graph answers 202 with an empty body; any HTTP error raises in the
        # transport, so reaching here means the send was accepted.
        self._transport(f"{GRAPH_BASE}/me/sendMail", data=payload, headers=headers)
