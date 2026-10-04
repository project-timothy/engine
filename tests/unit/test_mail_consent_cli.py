"""``engine mail consent <tenant>``: the owner's device-code consent for the
Graph mailbox, run from the engine instead of the retired pipeline repo.

Honesty audit 2026-09-03: the runbook and the ``GraphAuthError`` text told
the owner to "re-run the device-code consent", but the only tool that
performed it lived in a retired repository. The command must print the
verification URL and user code BEFORE it blocks on the sign-in, persist the
serialized MSAL cache to the keychain entry named by tenant config, and then
prove the provider can acquire silently from that entry. Nothing
token-shaped may reach stdout or stderr.

A fake ``msal`` module and a fake ``keyring`` module stand in via
``sys.modules``; the adapter imports both lazily, so the tests touch no
keychain and no network.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from core.adapters.graph_mail import GraphAuthError, keychain_token_provider
from core.engine.cli import main

ACCESS_SECRET = "ACCESS-TOKEN-eyJ0eXAiOiJKV1QiLCJhbGc"
REFRESH_SECRET = "REFRESH-TOKEN-0.AXoA9Q3mYq"
ID_SECRET = "ID-TOKEN-eyJhbGciOiJSUzI1NiIs"
USERNAME = "owner@example.test"
FLOW = {
    "user_code": "GJ7WK4HR",
    "device_code": "DEVICE-CODE-SECRET-1234",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "interval": 5,
    "message": "To sign in, use a web browser to open the page "
    "https://microsoft.com/devicelogin and enter the code GJ7WK4HR to authenticate.",
}
SUCCESS = {
    "access_token": ACCESS_SECRET,
    "refresh_token": REFRESH_SECRET,
    "id_token": ID_SECRET,
    "expires_in": 3599,
    "token_type": "Bearer",
    "id_token_claims": {"preferred_username": USERNAME, "name": "Owner"},
}
EXPIRED = {
    "error": "expired_token",
    "error_description": "AADSTS70020: The provided value for the input parameter "
    "'device_code' is not valid. The device code has expired.",
}
SECRETS = (ACCESS_SECRET, REFRESH_SECRET, ID_SECRET, FLOW["device_code"])


class FakeCache:
    """The subset of msal.SerializableTokenCache the adapter uses."""

    def __init__(self) -> None:
        self.accounts: list[dict] = []
        self.refresh: str | None = None
        self.has_state_changed = False

    def serialize(self) -> str:
        return json.dumps({"accounts": self.accounts, "refresh": self.refresh})

    def deserialize(self, raw: str) -> None:
        data = json.loads(raw)
        self.accounts = data["accounts"]
        self.refresh = data["refresh"]


def install_fakes(monkeypatch, *, device_result: dict, silent_ok: bool = True) -> dict:
    """Install fake ``msal`` and ``keyring`` modules. Returns a log dict the
    fakes write into: keyring store, app constructions, call order."""
    log: dict = {"store": {}, "apps": [], "calls": []}

    class FakePublicClientApplication:
        def __init__(self, client_id, authority=None, token_cache=None):
            log["apps"].append({"client_id": client_id, "authority": authority})
            self.cache = token_cache

        def initiate_device_flow(self, scopes):
            log["calls"].append(("initiate", tuple(scopes)))
            return dict(FLOW)

        def acquire_token_by_device_flow(self, flow):
            log["calls"].append(("device_flow", flow["user_code"]))
            log["stdout_at_wait"] = log["snapshot"]()
            if "access_token" in device_result:
                self.cache.accounts = [{"username": USERNAME, "home_account_id": "h.t"}]
                self.cache.refresh = REFRESH_SECRET
                self.cache.has_state_changed = True
            return dict(device_result)

        def get_accounts(self):
            return list(self.cache.accounts)

        def acquire_token_silent(self, scopes, account=None):
            log["calls"].append(("silent", account["username"] if account else None))
            if silent_ok and self.cache.refresh == REFRESH_SECRET:
                return {"access_token": ACCESS_SECRET, "expires_in": 3599}
            return {"error": "invalid_grant", "error_description": "refresh token revoked"}

    msal = types.ModuleType("msal")
    msal.SerializableTokenCache = FakeCache
    msal.PublicClientApplication = FakePublicClientApplication
    keyring = types.ModuleType("keyring")
    keyring.get_password = lambda service, account: log["store"].get((service, account))
    keyring.set_password = lambda service, account, value: log["store"].__setitem__(
        (service, account), value
    )
    monkeypatch.setitem(sys.modules, "msal", msal)
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    log["snapshot"] = lambda: ""
    return log


@pytest.fixture
def tenants_root(tmp_path, monkeypatch):
    root = tmp_path / "tenants"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "tenant.toml").write_text(
        "\n".join(
            [
                "[identity]",
                'legal_name = "Acme Test LLC"',
                'slug = "acme"',
                "",
                "[mail]",
                'client_id = "11111111-2222-3333-4444-555555555555"',
                'tenant_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
                'scopes = ["Mail.ReadWrite", "Mail.Send"]',
                'keychain_service = "svc.test.mailbox"',
                f'keychain_account = "{USERNAME}"',
                "",
            ]
        )
    )
    (root / "bare").mkdir()
    (root / "bare" / "tenant.toml").write_text(
        '[identity]\nlegal_name = "Bare LLC"\nslug = "bare"\n'
    )
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    return root


def _assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def test_consent_prints_code_first_persists_cache_and_provider_acquires_silently(
    tenants_root, monkeypatch, capsys
):
    log = install_fakes(monkeypatch, device_result=SUCCESS)
    # The fake snapshots stdout at the moment the flow starts blocking, so
    # the test can prove the owner saw the code before the wait began.
    snapshots: list[str] = []

    def snapshot() -> str:
        text = capsys.readouterr().out
        snapshots.append(text)
        return text

    log["snapshot"] = snapshot

    code = main(["mail", "consent", "acme"])
    captured = capsys.readouterr()
    before_wait = snapshots[0]
    after = captured.out

    assert code == 0, captured.err
    # The URL and the code reached stdout before the blocking wait.
    assert FLOW["verification_uri"] in before_wait
    assert FLOW["user_code"] in before_wait
    assert log["calls"][0] == ("initiate", ("Mail.ReadWrite", "Mail.Send"))
    assert log["calls"][1] == ("device_flow", FLOW["user_code"])
    # The account and the expiry are the only facts printed afterwards.
    assert USERNAME in after
    assert "expires" in after
    # The cache landed under the keychain names from tenant config.
    assert list(log["store"]) == [("svc.test.mailbox", USERNAME)]
    stored = json.loads(log["store"][("svc.test.mailbox", USERNAME)])
    assert stored["accounts"][0]["username"] == USERNAME
    # The command verified by acquiring silently from what it stored.
    assert ("silent", USERNAME) in log["calls"]
    assert "verified" in after
    # The MSAL app was built the way the provider builds it.
    authorities = {a["authority"] for a in log["apps"]}
    assert authorities == {"https://login.microsoftonline.com/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    assert {a["client_id"] for a in log["apps"]} == {"11111111-2222-3333-4444-555555555555"}
    # Nothing token-shaped on either stream.
    _assert_no_secret(before_wait + after + captured.err)

    # And the production provider, pointed at the same keychain names,
    # acquires silently from what the command persisted.
    provider = keychain_token_provider(
        client_id="11111111-2222-3333-4444-555555555555",
        tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        scopes=["Mail.ReadWrite", "Mail.Send"],
        keychain_service="svc.test.mailbox",
        keychain_account=USERNAME,
    )
    assert provider() == ACCESS_SECRET


def test_consent_timeout_exits_one_with_msal_error_and_stores_nothing(
    tenants_root, monkeypatch, capsys
):
    log = install_fakes(monkeypatch, device_result=EXPIRED)
    code = main(["mail", "consent", "acme"])
    captured = capsys.readouterr()
    assert code == 1
    assert "expired_token" in captured.err
    assert "The device code has expired" in captured.err
    assert log["store"] == {}
    assert ("silent", USERNAME) not in log["calls"]
    assert "verified" not in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_consent_that_cannot_be_acquired_silently_exits_one(tenants_root, monkeypatch, capsys):
    """Persisting is not proof: the command reports the silent verification
    failure instead of claiming the mailbox is ready."""
    log = install_fakes(monkeypatch, device_result=SUCCESS, silent_ok=False)
    code = main(["mail", "consent", "acme"])
    captured = capsys.readouterr()
    assert code == 1
    assert "silent token acquisition failed" in captured.err
    assert "refresh token revoked" in captured.err
    assert "verified" not in captured.out
    assert list(log["store"]) == [("svc.test.mailbox", USERNAME)]
    _assert_no_secret(captured.out + captured.err)


def test_consent_flow_that_fails_to_start_exits_one(tenants_root, monkeypatch, capsys):
    log = install_fakes(monkeypatch, device_result=SUCCESS)
    monkeypatch.setattr(
        sys.modules["msal"].PublicClientApplication,
        "initiate_device_flow",
        lambda self, scopes: {"error": "invalid_client", "error_description": "AADSTS700016"},
    )
    code = main(["mail", "consent", "acme"])
    captured = capsys.readouterr()
    assert code == 1
    assert "AADSTS700016" in captured.err
    assert log["store"] == {}


def test_consent_refuses_a_tenant_without_mail_config(tenants_root, monkeypatch, capsys):
    install_fakes(monkeypatch, device_result=SUCCESS)
    code = main(["mail", "consent", "bare"])
    captured = capsys.readouterr()
    assert code == 2
    assert "[mail]" in captured.err


def test_consent_unknown_tenant_exits_two(tenants_root, monkeypatch, capsys):
    install_fakes(monkeypatch, device_result=SUCCESS)
    code = main(["mail", "consent", "nobody"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no tenant config" in captured.err


def test_provider_error_names_the_engine_command(monkeypatch):
    """The provider's own failure text points at the engine command, never at
    a retired repository."""
    install_fakes(monkeypatch, device_result=SUCCESS)
    provider = keychain_token_provider(
        client_id="c",
        tenant_id="t",
        scopes=["Mail.Read"],
        keychain_service="svc.empty",
        keychain_account=USERNAME,
    )
    with pytest.raises(GraphAuthError, match="engine mail consent"):
        provider()


def test_an_extra_scope_rides_the_same_sign_in_and_mail_scopes_stay(
    tenants_root, monkeypatch, capsys
):
    """deadlines/calendar asks for Calendars.ReadWrite on its own; the owner
    grants it in the same device-code sign-in, and [mail].scopes is unchanged
    so no mail job ever waits on the calendar (2026-10-04)."""
    log = install_fakes(monkeypatch, device_result=SUCCESS)
    log["snapshot"] = lambda: capsys.readouterr().out
    code = main(
        ["mail", "consent", "acme", "--scope", "Calendars.ReadWrite", "--scope", "Mail.Send"]
    )
    assert code == 0, capsys.readouterr().err
    assert log["calls"][0] == ("initiate", ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"))
