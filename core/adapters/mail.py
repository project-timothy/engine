"""The one place a mailbox adapter is chosen: ``[mail].provider``.

Every job that reads or writes mail asks here and never names a provider
(the mail seam, ``core/contracts/mail.py``). ``graph`` builds the Microsoft
Graph client over an MSAL cache in the keychain; ``gmail`` builds the Gmail
client over a refresh token in the keychain plus the client secret named by
``[mail].client_secret_env``. An empty provider is refused by name: the
engine ships neither as the default, an adopter picks (extraction plan,
gate 3).

``eager=True`` acquires the token here, before the client is handed back,
for the caller that must know it holds a credential BEFORE it stamps
anything (the close's send: issue #135, honesty audit F11). A missing or
expired consent then raises :class:`MailAuthError` with nothing attempted.
"""

from __future__ import annotations

import os

from ..contracts.mail import MailAuthError, MailClient, MailError
from ..engine.config import MailSettings

PROVIDERS = ("graph", "gmail")


class MailProviderError(MailError):
    """``[mail].provider`` is empty or names nothing this engine ships."""


def client_for(mail: MailSettings, *, eager: bool = False) -> MailClient:
    provider = mail.provider.strip().lower()
    if not provider:
        raise MailProviderError(
            "[mail].provider is empty: name the mailbox provider, one of "
            f"{', '.join(PROVIDERS)} (the engine ships neither as the default)"
        )
    if provider == "graph":
        from .graph_mail import GraphMailClient, keychain_token_provider

        token_provider = keychain_token_provider(
            client_id=mail.client_id,
            tenant_id=mail.tenant_id,
            scopes=list(mail.scopes),
            keychain_service=mail.keychain_service,
            keychain_account=mail.keychain_account,
        )
        if eager:
            token = token_provider()
            return GraphMailClient(token_provider=lambda: token)
        return GraphMailClient(token_provider=token_provider)
    if provider == "gmail":
        from .gmail import GmailClient, refresh_token_provider

        secret = os.environ.get(mail.client_secret_env, "") if mail.client_secret_env else ""
        if not secret:
            raise MailAuthError(
                "[mail].client_secret_env "
                + (
                    f"{mail.client_secret_env} is not set on this host"
                    if mail.client_secret_env
                    else "is empty: name the environment variable carrying the OAuth client secret"
                )
            )
        token_provider = refresh_token_provider(
            client_id=mail.client_id,
            client_secret=secret,
            keychain_service=mail.keychain_service,
            keychain_account=mail.keychain_account,
        )
        if eager:
            token = token_provider()
            return GmailClient(token_provider=lambda: token)
        return GmailClient(token_provider=token_provider)
    raise MailProviderError(f"[mail].provider {provider!r} is not one of {', '.join(PROVIDERS)}")
