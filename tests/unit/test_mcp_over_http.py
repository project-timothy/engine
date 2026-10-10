"""The read tools over HTTP, signed in as a person (Tim's front door, step 3).

A missionary's phone cannot launch `engine mcp` on stdio; Claude, ChatGPT and
Tim's own text box reach a hosted Tim box over HTTPS. The promises under test,
from the MCP Streamable HTTP binding (2025-06-18 and 2025-11-25 revisions,
served statelessly: no session ids) and its authorization spec:

1. Nothing is served without a bearer token the box's verifier accepts; a
   missing or bad token gets 401 with a WWW-Authenticate challenge naming the
   Protected Resource Metadata document (RFC 9728), which itself is public.
2. The token names a person, and every answer is that person's (the Viewer
   from #458): Ruth sees her own report and never the Bakers'.
3. A request whose Origin is not the box's own is refused (403), against
   DNS rebinding. GET and DELETE on the endpoint are 405 (no streams, no
   sessions). A notification is 202 with no body.
4. A 2026-07-28 request (no initialize, per-request _meta) gets a 400 that is
   not a recognized modern error, so a dual-era client falls back to
   initialize, the era this endpoint speaks.

Until a sign-in service is chosen (a one-way door), the only verifier that
admits anyone is a token file of hashed invitation tokens; the default admits
nobody.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from core.tools.mcp_http import (
    PRM_PATH,
    RefuseAll,
    TokenFile,
    make_server,
    new_token,
)
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)

RESOURCE = "https://tim.example.org/mcp"


@pytest.fixture
def box(church, tmp_path):  # noqa: F811
    tokens = tmp_path / "door-tokens.json"
    ruth = new_token(tokens, "ruth-hollis")
    carol = new_token(tokens, "carol-jennings")
    server = make_server(
        "grace",
        tenants_root=church["root"],
        ledger_root=church["ledger_root"],
        obligations_file=church["obligations"],
        verifier=TokenFile(tokens),
        resource=RESOURCE,
        authorization_servers=["https://signin.example.org"],
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"base": base, "ruth": ruth, "carol": carol}
    server.shutdown()
    server.server_close()


def _post(box, body, token=None, headers=None):
    request = urllib.request.Request(
        box["base"] + "/mcp",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def _rpc(box, token, method, params=None, mid=1):
    status, _h, raw = _post(
        box,
        {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
        token,
        {"MCP-Protocol-Version": "2025-06-18"},
    )
    assert status == 200, raw
    return json.loads(raw)


def test_nothing_is_served_without_a_token(box):
    status, headers, _raw = _post(box, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 401
    challenge = headers.get("WWW-Authenticate", "")
    assert challenge.startswith("Bearer ")
    assert "resource_metadata=" in challenge and PRM_PATH in challenge


def test_a_bad_token_is_refused_as_invalid(box):
    status, headers, _raw = _post(box, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, "nope")
    assert status == 401
    assert 'error="invalid_token"' in headers.get("WWW-Authenticate", "")


def test_the_protected_resource_metadata_is_public(box):
    with urllib.request.urlopen(box["base"] + PRM_PATH, timeout=10) as resp:
        meta = json.loads(resp.read())
    assert meta["resource"] == RESOURCE
    assert meta["authorization_servers"] == ["https://signin.example.org"]
    assert meta["bearer_methods_supported"] == ["header"]


def test_ruth_signed_in_sees_her_own_and_never_the_bakers(box):
    init = _rpc(box, box["ruth"], "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in _rpc(box, box["ruth"], "tools/list")["result"]["tools"]}
    assert "close_status" not in names
    reply = _rpc(box, box["ruth"], "tools/call", {"name": "expense_reports", "arguments": {}})
    rows = json.loads(reply["result"]["content"][0]["text"])["rows"]
    assert [r["person"] for r in rows] == ["Ruth Hollis"]


def test_the_treasurer_signed_in_sees_the_church(box):
    reply = _rpc(box, box["carol"], "tools/call", {"name": "expense_reports", "arguments": {}})
    rows = json.loads(reply["result"]["content"][0]["text"])["rows"]
    assert {r["person"] for r in rows} == {"Ruth Hollis", "Tom and Jen Baker"}


def test_a_foreign_origin_is_refused(box):
    status, _h, _raw = _post(
        box,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        box["ruth"],
        {"Origin": "https://evil.example.com"},
    )
    assert status == 403


def test_get_and_delete_are_not_allowed(box):
    for method in ("GET", "DELETE"):
        request = urllib.request.Request(
            box["base"] + "/mcp", method=method, headers={"Authorization": f"Bearer {box['ruth']}"}
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 405


def test_a_notification_is_accepted_with_no_body(box):
    status, _h, raw = _post(
        box, {"jsonrpc": "2.0", "method": "notifications/initialized"}, box["ruth"]
    )
    assert status == 202 and raw == b""


def test_a_modern_request_is_told_to_fall_back_to_initialize(box):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    }
    status, _h, raw = _post(box, body, box["ruth"], {"MCP-Protocol-Version": "2026-07-28"})
    assert status == 400
    error = json.loads(raw)["error"]
    assert error["code"] not in (-32020, -32021, -32022)  # not a recognized modern error
    assert "initialize" in error["message"]


def test_a_token_whose_person_left_authority_toml_is_refused(box, church, tmp_path):  # noqa: F811
    tokens = tmp_path / "door-tokens.json"
    ghost = new_token(tokens, "someone-removed")
    status, _h, _raw = _post(box, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, ghost)
    assert status == 403


def test_the_default_verifier_admits_nobody():
    assert RefuseAll().person("anything") is None


def test_a_token_file_keeps_only_hashes(tmp_path):
    tokens = tmp_path / "t.json"
    token = new_token(tokens, "ruth-hollis")
    assert token not in tokens.read_text()
    assert TokenFile(tokens).person(token) == "ruth-hollis"
    assert TokenFile(tokens).person(token + "x") is None


def test_door_token_invites_a_person_and_refuses_a_stranger(church, tmp_path, capsys):  # noqa: F811
    from core.engine.cli import main

    tokens = tmp_path / "invites.json"
    assert main(["door-token", "grace", "ruth-hollis", "--tokens", str(tokens)]) == 0
    token = capsys.readouterr().out.strip()
    assert TokenFile(tokens).person(token) == "ruth-hollis"
    assert main(["door-token", "grace", "nobody", "--tokens", str(tokens)]) == 2
    assert "nobody" in capsys.readouterr().err


def test_http_needs_the_public_url(church, capsys):  # noqa: F811
    from core.engine.cli import main

    assert main(["mcp", "grace", "--http"]) == 2
    assert "--resource" in capsys.readouterr().err
