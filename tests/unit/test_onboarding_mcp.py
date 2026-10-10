"""Onboarding over MCP (issue #448, docs/tenant-kit-design.md section 6).

`engine onboard` speaks JSON for an agent with a shell. A chat client with no
shell needs the same conversation over MCP, and onboarding runs before a
tenant (or a ledger) exists, so this surface is tenantless: `engine mcp
--onboarding`. Four tools: the next question, record an answer, preview the
plan, and apply it. Apply writes authority.toml, and the floor says a person
confirms an authority change, so apply refuses unless ``confirm`` repeats the
slug, which the agent passes only after the person says yes. The answers file
is the one `engine onboard` uses, so the two surfaces can hand off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.engine.config import load_tenant
from core.onboarding.mcp import OnboardingTools
from core.tools.mcp_stdio import McpServer

REPO = Path(__file__).resolve().parents[2]

ELLIS = {"who": "nonprofit-solo", "legal_name": "Ellis Mission Inc.", "your_name": "Jo Ellis"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    tools = OnboardingTools(answers_dir=tmp_path, root=root, run_audit=False)
    return tmp_path, root, tools


def _rpc(server: McpServer, method: str, params: dict | None = None, mid: int = 1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def _call(server: McpServer, name: str, **arguments) -> tuple[dict | str, bool]:
    result = _rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        return text, True
    return json.loads(text), False


def _walk(server: McpServer, slug: str, values: dict) -> dict:
    """Answer every question as a chat agent would: ask, take the person's
    answer or the default, record it."""
    out, err = _call(server, "onboarding_next", slug=slug)
    assert not err, out
    while out["next"] is not None:
        q = out["next"]
        out, err = _call(
            server,
            "onboarding_record",
            slug=slug,
            id=q["id"],
            value=values.get(q["id"], q.get("default", "")),
        )
        assert not err, out
    return out


def test_the_server_lists_four_tools_and_only_record_and_apply_write(world):
    _tmp, _root, tools = world
    server = McpServer(tools, name=tools.server_name, instructions=tools.instructions)
    init = _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "timothy-onboarding"
    assert "confirm" in init["result"]["instructions"]
    listed = {t["name"]: t for t in _rpc(server, "tools/list")["result"]["tools"]}
    assert set(listed) == {
        "onboarding_next",
        "onboarding_record",
        "onboarding_plan",
        "onboarding_apply",
    }
    read_only = {n for n, t in listed.items() if t["annotations"]["readOnlyHint"]}
    assert read_only == {"onboarding_next", "onboarding_plan"}
    assert "confirm" in listed["onboarding_apply"]["inputSchema"]["required"]


def test_the_tenant_tools_stay_read_only(world):
    """The tenant server's annotations are unchanged by the new field."""
    from core.tools import catalog

    server = McpServer(catalog.Tools("demo", ledger_root=Path("/nonexistent")))
    for tool in _rpc(server, "tools/list")["result"]["tools"]:
        assert tool["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
    init = _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "timothy-engine"


def test_a_chat_agent_walks_the_conversation(world):
    tmp, _root, tools = world
    server = McpServer(tools)
    first, err = _call(server, "onboarding_next", slug="ellis")
    assert not err
    assert first["next"]["id"] == "who"
    done = _walk(server, "ellis", ELLIS)
    assert done["next"] is None
    assert done["missing"] == []
    saved = json.loads((tmp / "onboarding-ellis.json").read_text())
    assert saved["who"] == "nonprofit-solo"


def test_a_bad_answer_is_an_error_the_agent_can_read(world):
    _tmp, _root, tools = world
    server = McpServer(tools)
    text, err = _call(server, "onboarding_record", slug="ellis", id="who", value="a spaceship")
    assert err
    assert "who" in text
    text, err = _call(server, "onboarding_record", slug="ellis", id="legal_name", value="X")
    assert err
    assert "who this is for first" in text


def test_the_plan_previews_in_plain_terms_and_writes_nothing(world):
    _tmp, root, tools = world
    server = McpServer(tools)
    _walk(server, "ellis", ELLIS)
    preview, err = _call(server, "onboarding_plan", slug="ellis")
    assert not err
    assert preview["plan"]["legal_name"] == "Ellis Mission Inc."
    assert preview["plan"]["people"] == [{"name": "Jo Ellis", "role": "missionary"}]
    assert preview["confirm_with"] == "ellis"
    assert not (root / "ellis").exists()


def test_the_plan_names_what_is_still_owed(world):
    _tmp, _root, tools = world
    server = McpServer(tools)
    _call(server, "onboarding_record", slug="acme", id="who", value="commercial-small")
    text, err = _call(server, "onboarding_plan", slug="acme")
    assert err
    assert "legal_name" in text


@pytest.mark.parametrize("confirm", [None, "", "yes", True, "ELLIS", "other"])
def test_apply_refuses_without_the_slug_repeated(world, confirm):
    _tmp, root, tools = world
    server = McpServer(tools)
    _walk(server, "ellis", ELLIS)
    args = {"slug": "ellis"} if confirm is None else {"slug": "ellis", "confirm": confirm}
    result = _rpc(server, "tools/call", {"name": "onboarding_apply", "arguments": args})["result"]
    assert result["isError"] is True
    assert "confirm" in result["content"][0]["text"]
    assert not (root / "ellis").exists()


def test_apply_with_the_persons_yes_builds_the_tenant(world):
    _tmp, root, tools = world
    server = McpServer(tools)
    _walk(server, "ellis", ELLIS)
    out, err = _call(server, "onboarding_apply", slug="ellis", confirm="ellis")
    assert not err, out
    assert out["created"].endswith("ellis")
    assert any(line.startswith("kit authority") for line in out["doctor"])
    assert load_tenant("ellis", tenants_root=root).identity.shape == "nonprofit-solo"


def test_apply_refuses_a_tenant_that_exists(world):
    _tmp, _root, tools = world
    server = McpServer(tools)
    _walk(server, "ellis", ELLIS)
    _call(server, "onboarding_apply", slug="ellis", confirm="ellis")
    text, err = _call(server, "onboarding_apply", slug="ellis", confirm="ellis")
    assert err
    assert "ellis" in text


@pytest.mark.parametrize("slug", ["../evil", "Ellis", "", "a/b", "a b"])
def test_a_slug_that_is_not_a_slug_never_touches_the_disk(world, slug):
    tmp, _root, tools = world
    server = McpServer(tools)
    text, err = _call(server, "onboarding_record", slug=slug, id="who", value="nonprofit-solo")
    assert err
    assert "slug" in text
    assert sorted(p.name for p in tmp.iterdir()) == []


def test_the_cli_and_mcp_share_one_answers_file(world, capsys):
    from core.engine.cli import main as engine_main

    tmp, root, tools = world
    assert (
        engine_main(["onboard", "ellis", "--root", str(root), "--answer", "who=nonprofit-solo"])
        == 0
    )
    capsys.readouterr()
    out, err = _call(McpServer(tools), "onboarding_next", slug="ellis")
    assert not err
    assert out["answers"] == {"who": "nonprofit-solo"}
    assert out["next"]["id"] == "legal_name"
    assert (tmp / "onboarding-ellis.json").is_file()


def test_engine_mcp_onboarding_speaks_over_stdio_with_no_tenant(tmp_path):
    lines = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "onboarding_next", "arguments": {"slug": "ellis"}},
        },
    ]
    env = {
        **os.environ,
        "ENGINE_TENANTS_ROOT": str(tmp_path / "tenants"),
        "ENGINE_LEDGER_ROOT": str(tmp_path / "ledger"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.engine.cli",
            "mcp",
            "--onboarding",
            "--answers-dir",
            str(tmp_path),
        ],
        input="\n".join(json.dumps(x) for x in lines) + "\n",
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=60,
    )
    replies = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    assert [r["id"] for r in replies] == [1, 2], proc.stderr
    assert replies[0]["result"]["serverInfo"]["name"] == "timothy-onboarding"
    body = json.loads(replies[1]["result"]["content"][0]["text"])
    assert body["next"]["id"] == "who"


def test_engine_mcp_needs_a_tenant_or_onboarding(capsys):
    from core.engine.cli import main as engine_main

    assert engine_main(["mcp"]) == 2
    assert "--onboarding" in capsys.readouterr().err
