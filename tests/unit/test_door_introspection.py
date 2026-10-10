"""The HTTP door asks the box's sign-in service who a token is (RFC 7662).

The sign-in service (the doorkeeper, github.com/project-timothy/doorkeeper)
is its own program on the Tim box and never part of the engine; the engine
learns who is asking only by POSTing the token to its introspection endpoint
with the box's shared secret. The promises under test:

1. An active access token for this endpoint's resource names its person.
2. Anything else is nobody: inactive, another resource, a refresh token, an
   expired one, an unreachable or confused service, an empty token.
3. The token travels in the body and the secret in the header; never in a
   URL. The service is reached over https, or http on loopback only.
4. ``engine mcp --http --introspect URL --introspect-secret FILE`` admits by
   it; an invitation-token file and a sign-in service never mix.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from core.tools.mcp_http import Introspection, make_server
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)

RESOURCE = "https://tim.example.org/mcp"
SECRET = "s" * 48


class _Service(BaseHTTPRequestHandler):
    answers: dict[str, object] = {}
    seen: list[dict[str, object]] = []

    def do_POST(self):  # noqa: N802  the base class's name
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        type(self).seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self.send_response(401)
            self.end_headers()
            return
        token = parse_qs(body).get("token", [""])[0]
        reply = type(self).answers.get(token, {"active": False})
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def _active(person, **over):
    reply = {
        "active": True,
        "sub": person,
        "aud": RESOURCE,
        "token_type": "access_token",
        "exp": int(time.time()) + 600,
    }
    reply.update(over)
    return reply


@pytest.fixture
def service(tmp_path):
    _Service.answers = {
        "ruth-token": _active("ruth-hollis"),
        "carol-token": _active("carol-jennings", aud=[RESOURCE, "https://other.example"]),
        "elsewhere": _active("ruth-hollis", aud="https://other.example/mcp"),
        "refresh": _active("ruth-hollis", token_type="refresh_token"),
        "expired": _active("ruth-hollis", exp=int(time.time()) - 5),
        "no-sub": _active(""),
        "garbled": b"not json",
        "active-string": {"active": "true", "sub": "ruth-hollis", "aud": RESOURCE},
    }
    _Service.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    secret = tmp_path / "introspect.secret"
    secret.write_text(SECRET + "\n")
    url = f"http://127.0.0.1:{server.server_address[1]}/introspect"
    yield {"url": url, "secret": secret}
    server.shutdown()
    server.server_close()


def _verifier(service, **over):
    args = {"url": service["url"], "secret_file": service["secret"], "resource": RESOURCE}
    args.update(over)
    return Introspection(**args)


def test_an_active_token_for_this_resource_names_its_person(service):
    assert _verifier(service).person("ruth-token") == "ruth-hollis"
    assert _verifier(service).person("carol-token") == "carol-jennings"


@pytest.mark.parametrize(
    "token",
    ["unknown", "elsewhere", "refresh", "expired", "no-sub", "garbled", "active-string"],
)
def test_anything_else_is_nobody(service, token):
    assert _verifier(service).person(token) is None


def test_an_empty_token_is_nobody_without_asking(service):
    assert _verifier(service).person("") is None
    assert _Service.seen == []


def test_the_token_rides_in_the_body_and_the_secret_in_the_header(service):
    _verifier(service).person("ruth-token")
    (asked,) = _Service.seen
    assert asked["path"] == "/introspect"
    assert asked["auth"] == f"Bearer {SECRET}"
    assert parse_qs(str(asked["body"])) == {"token": ["ruth-token"]}


def test_a_wrong_secret_is_nobody(service, tmp_path):
    wrong = tmp_path / "wrong.secret"
    wrong.write_text("w" * 48)
    assert _verifier(service, secret_file=wrong).person("ruth-token") is None


def test_an_unreachable_service_is_nobody(tmp_path):
    secret = tmp_path / "s"
    secret.write_text(SECRET)
    gone = Introspection(url="http://127.0.0.1:9/introspect", secret_file=secret, resource=RESOURCE)
    assert gone.person("ruth-token") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://signin.example.org/introspect",
        "ftp://127.0.0.1/introspect",
        "https://127.0.0.1/introspect?token=x",
    ],
)
def test_the_service_is_https_or_loopback_http(url, tmp_path):
    secret = tmp_path / "s"
    secret.write_text(SECRET)
    with pytest.raises(ValueError):
        Introspection(url=url, secret_file=secret, resource=RESOURCE)


def test_a_short_secret_is_refused(tmp_path):
    secret = tmp_path / "s"
    secret.write_text("short")
    with pytest.raises(ValueError):
        Introspection(url="http://127.0.0.1:1/x", secret_file=secret, resource=RESOURCE)


def test_ruth_signed_in_through_the_service_sees_her_own(service, church):  # noqa: F811
    server = make_server(
        "grace",
        tenants_root=church["root"],
        ledger_root=church["ledger_root"],
        obligations_file=church["obligations"],
        verifier=_verifier(service),
        resource=RESOURCE,
        authorization_servers=["https://tim.example.org"],
        host="127.0.0.1",
        port=0,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "expense_reports", "arguments": {}},
                }
            ).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer ruth-token",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310  loopback
            text = json.loads(resp.read())["result"]["content"][0]["text"]
    finally:
        server.shutdown()
        server.server_close()
    assert [r["person"] for r in json.loads(text)["rows"]] == ["Ruth Hollis"]


def test_http_takes_a_sign_in_service_or_a_token_file_never_both(church, capsys):  # noqa: F811
    from core.engine.cli import main

    base = ["mcp", "grace", "--http", "--resource", RESOURCE]
    assert main([*base, "--introspect", "http://127.0.0.1:1/introspect"]) == 2
    assert "--introspect-secret" in capsys.readouterr().err
    both = [*base, "--tokens", "t.json", "--introspect", "http://127.0.0.1:1/x"]
    assert main([*both, "--introspect-secret", "s"]) == 2
    assert "not both" in capsys.readouterr().err
