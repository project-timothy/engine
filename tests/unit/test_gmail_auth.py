"""Gmail auth for the mail seam: the refresh-token provider and the loopback
consent, driven through fake transports and a fake keyring. Nothing here
touches a socket except the one test that proves the loopback listener
itself, on 127.0.0.1 with an ephemeral port.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import types
import urllib.parse
import urllib.request

import pytest

from core.adapters import gmail
from core.adapters.gmail import GmailAuthError, loopback_consent, refresh_token_provider

USERNAME = "owner@example.test"


@pytest.fixture
def keyring_store(monkeypatch) -> dict:
    store: dict = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, account: store.get((service, account))
    fake.set_password = lambda service, account, value: store.__setitem__((service, account), value)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return store


def _id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"


class FormTransport:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.posts: list[tuple[str, dict]] = []

    def __call__(self, url, *, data=None, headers=None):
        self.posts.append((url, dict(urllib.parse.parse_qsl(data.decode()))))
        return self.reply


def test_the_provider_trades_the_keychain_refresh_token_for_an_access_token(keyring_store):
    keyring_store[("svc", USERNAME)] = json.dumps({"refresh_token": "R-1"})
    transport = FormTransport({"access_token": "A-1", "expires_in": 3599})
    provider = refresh_token_provider(
        client_id="c",
        client_secret="s",
        keychain_service="svc",
        keychain_account=USERNAME,
        transport=transport,
    )
    assert provider() == "A-1"
    url, form = transport.posts[0]
    assert url == gmail.TOKEN_URL
    assert form == {
        "client_id": "c",
        "client_secret": "s",
        "refresh_token": "R-1",
        "grant_type": "refresh_token",
    }


def test_a_missing_refresh_token_names_the_engine_command(keyring_store):
    provider = refresh_token_provider(
        client_id="c", client_secret="s", keychain_service="svc", keychain_account=USERNAME
    )
    with pytest.raises(GmailAuthError, match="engine mail consent"):
        provider()
    keyring_store[("svc", USERNAME)] = "not json"
    with pytest.raises(GmailAuthError, match="engine mail consent"):
        provider()


def test_a_refused_exchange_is_an_auth_error(keyring_store):
    keyring_store[("svc", USERNAME)] = json.dumps({"refresh_token": "R-1"})
    transport = FormTransport({"error": "invalid_grant", "error_description": "revoked"})
    provider = refresh_token_provider(
        client_id="c",
        client_secret="s",
        keychain_service="svc",
        keychain_account=USERNAME,
        transport=transport,
    )
    with pytest.raises(GmailAuthError, match="revoked"):
        provider()


def test_consent_prompts_the_url_first_stores_only_the_refresh_token_and_names_the_user(
    keyring_store,
):
    prompted: list[str] = []
    transport = FormTransport(
        {
            "access_token": "A-1",
            "refresh_token": "R-1",
            "expires_in": 3599,
            "id_token": _id_token(USERNAME),
        }
    )
    result = loopback_consent(
        client_id="c",
        client_secret="s",
        scopes=["scope-a", "scope-b"],
        keychain_service="svc",
        keychain_account=USERNAME,
        prompt=prompted.append,
        code_source=lambda url: "code-1",
        transport=transport,
        port=8765,
    )
    assert result.username == USERNAME and result.expires_in == 3599
    query = urllib.parse.parse_qs(urllib.parse.urlparse(prompted[0]).query)
    assert query["scope"] == ["scope-a scope-b"]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/"]
    _, form = transport.posts[0]
    assert form["code"] == "code-1" and form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == "http://127.0.0.1:8765/"
    assert keyring_store == {("svc", USERNAME): json.dumps({"refresh_token": "R-1"})}


def test_consent_stores_nothing_when_the_exchange_yields_no_refresh_token(keyring_store):
    transport = FormTransport({"access_token": "A-1", "expires_in": 3599})
    with pytest.raises(GmailAuthError, match="no refresh token"):
        loopback_consent(
            client_id="c",
            client_secret="s",
            scopes=["s"],
            keychain_service="svc",
            keychain_account=USERNAME,
            prompt=lambda url: None,
            code_source=lambda url: "code-1",
            transport=transport,
        )
    assert keyring_store == {}


def test_consent_refuses_an_empty_code(keyring_store):
    with pytest.raises(GmailAuthError, match="authorization code"):
        loopback_consent(
            client_id="c",
            client_secret="s",
            scopes=["s"],
            keychain_service="svc",
            keychain_account=USERNAME,
            prompt=lambda url: None,
            code_source=lambda url: "",
            transport=FormTransport({}),
        )


def test_the_loopback_listener_catches_the_browsers_redirect(keyring_store):
    """The real listener: bind an ephemeral port, hand the prompt the URL, and
    let a 'browser' (urllib, in this thread) hit the redirect with a code."""
    transport = FormTransport({"refresh_token": "R-2", "expires_in": 10})
    prompted: list[str] = []
    ready = threading.Event()

    def prompt(url: str) -> None:
        prompted.append(url)
        ready.set()

    outcome: dict = {}

    def run() -> None:
        try:
            outcome["result"] = loopback_consent(
                client_id="c",
                client_secret="s",
                scopes=["s"],
                keychain_service="svc",
                keychain_account=USERNAME,
                prompt=prompt,
                transport=transport,
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    assert ready.wait(5), "the consent never prompted"
    redirect = urllib.parse.parse_qs(urllib.parse.urlparse(prompted[0]).query)["redirect_uri"][0]
    with urllib.request.urlopen(redirect + "?code=code-9&state=x", timeout=5) as resp:
        assert resp.status == 200
    worker.join(5)
    assert "error" not in outcome, outcome.get("error")
    assert transport.posts[0][1]["code"] == "code-9"
    assert keyring_store[("svc", USERNAME)] == json.dumps({"refresh_token": "R-2"})
