"""The auditor's own mailbox listing: metadata only, never content.

A deliberate duplicate of the minimum the mail-coverage lens needs: list
messages-with-attachments in a window, with attachment names, sizes, and
inline flags via $expand — no attachment content is ever downloaded, no
message is ever modified. Auth is the same cached device-code consent the
engine uses (the design's second coordination point: msal's cache
serialization plus the hour gap between runs covers rotation), acquired
silently from the OS keychain with the auditor's own few lines.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

Transport = Callable[[str, dict], dict]
TokenProvider = Callable[[], str]


class AuditorGraphError(RuntimeError):
    pass


@dataclass
class ListedAttachment:
    name: str = ""
    size: int = 0
    is_inline: bool = False


@dataclass
class ListedMessage:
    sender: str = ""
    date: str = ""  # receivedDateTime, ISO
    attachments: list[ListedAttachment] = field(default_factory=list)


def keychain_token_provider(mail_config: dict) -> TokenProvider:
    """Silent MSAL acquisition from the keychain cache the tenant's one-time
    device-code consent populated. Rewrites the cache when msal rotates it —
    never lose a rotated credential."""

    def provider() -> str:
        import keyring  # lazy: evals never import these
        import msal

        service = str(mail_config.get("keychain_service", ""))
        account = str(mail_config.get("keychain_account", ""))
        cache = msal.SerializableTokenCache()
        stored = keyring.get_password(service, account)
        if not stored:
            raise AuditorGraphError(f"no MSAL token cache in keychain ({service})")
        cache.deserialize(stored)
        app = msal.PublicClientApplication(
            str(mail_config.get("client_id", "")),
            authority=f"https://login.microsoftonline.com/{mail_config.get('tenant_id', '')}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        if not accounts:
            raise AuditorGraphError("MSAL cache holds no account")
        result = app.acquire_token_silent(list(mail_config.get("scopes", [])), account=accounts[0])
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "no result")
            raise AuditorGraphError(f"silent token acquisition failed: {detail[:200]}")
        if cache.has_state_changed:
            keyring.set_password(service, account, cache.serialize())
        return result["access_token"]

    return provider


def _default_transport(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


class AuditorGraphClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport or _default_transport

    def list_messages_with_attachments(self, *, since_iso: str) -> list[ListedMessage]:
        """Every inbox message with attachments received on/after ``since_iso``,
        attachment metadata expanded, content never touched. Follows paging."""
        token = self._token_provider()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        params = urllib.parse.urlencode(
            {
                "$filter": f"hasAttachments eq true and receivedDateTime ge {since_iso}",
                "$select": "sender,receivedDateTime",
                "$expand": "attachments($select=name,size,isInline)",
                "$top": "50",
            }
        )
        url = f"{GRAPH_BASE}/me/mailFolders/Inbox/messages?{params}"
        messages: list[ListedMessage] = []
        while url:
            payload = self._transport(url, headers)
            for obj in payload.get("value", []):
                sender = ((obj.get("sender") or {}).get("emailAddress") or {}).get("address", "")
                messages.append(
                    ListedMessage(
                        sender=str(sender),
                        date=str(obj.get("receivedDateTime", "")),
                        attachments=[
                            ListedAttachment(
                                name=str(a.get("name", "")),
                                size=int(a.get("size", 0) or 0),
                                is_inline=bool(a.get("isInline", False)),
                            )
                            for a in obj.get("attachments", [])
                        ],
                    )
                )
            url = payload.get("@odata.nextLink", "")
        return messages
