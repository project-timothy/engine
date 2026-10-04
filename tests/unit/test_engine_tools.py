"""The engine's read-only tools, and the MCP server that offers them (Ask Tim, step 1).

Any chat front end (Claude Desktop today, Tim's own app later) reaches the
books through these tools. The promises under test:

1. Read only. The tools open the ledger read-only and never take the write
   lock, so a question mid-run can neither block nor be blocked by the 08:00 run.
2. Every money value is an exact decimal string, never a float, and every row
   names its source (table and id) so an answer can be traced to the books.
3. Shadow rows never appear.
4. The MCP layer speaks JSON-RPC 2.0 over stdio: initialize, tools/list,
   tools/call, ping; an unknown tool or method is an error, never a guess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.tools import catalog
from core.tools.mcp_stdio import McpServer

REPO = Path(__file__).resolve().parents[2]

OBLIGATIONS = """
[[obligation]]
id = "permit"
title = "Work permit renewal"
who = "Jane"
due = 2026-10-20
"""


@pytest.fixture
def books(tmp_path):
    ledger_dir = tmp_path / "data"
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="A-100",
            amount_cents=123456,
            due_date="2026-10-30",
            status="Received",
        )
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="A-099",
            amount_cents=50000,
            status="Paid",
        )
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Ghost Co",
            invoice_number="SH-1",
            amount_cents=999,
            status="Received",
            shadow=True,
        )
    ob = tmp_path / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    run("demo", "demo", "ingest", ledger_dir=ledger_dir)  # one real run row
    return {"ledger_dir": ledger_dir, "obligations": ob, "root": root}


def _tools(books, **kw):
    return catalog.Tools(
        "demo",
        ledger_root=books["root"],
        obligations_file=books["obligations"],
        today="2026-10-04",
        **kw,
    )


def test_open_payables_are_exact_sourced_and_never_shadow(books):
    rows = _tools(books).call("open_payables", {})["rows"]
    assert [r["invoice_number"] for r in rows] == ["A-100"]
    assert rows[0]["amount"] == "1234.56" and isinstance(rows[0]["amount"], str)
    assert rows[0]["source"].startswith("ap_invoices#")


def test_find_invoices_searches_vendor_and_number(books):
    rows = _tools(books).call("find_invoices", {"query": "acme"})["rows"]
    assert sorted(r["invoice_number"] for r in rows) == ["A-099", "A-100"]
    assert _tools(books).call("find_invoices", {"query": "ghost"})["rows"] == []


def test_deadlines_come_from_the_obligations_and_the_done_events(books):
    out = _tools(books).call("deadlines", {"days": 30})
    assert [(d["id"], d["due"], d["days_left"]) for d in out["rows"]] == [
        ("permit", "2026-10-20", 16)
    ]


def test_recent_runs_reports_the_last_run_per_job(books):
    rows = _tools(books).call("recent_runs", {})["rows"]
    assert any(r["agent"] == "demo" and r["job"] == "ingest" for r in rows)
    assert all("status" in r and "source" in r for r in rows)


def test_waiting_cards_lists_pending_only(books):
    with Ledger.open(books["root"]) as ledger:
        run_id = ledger.conn.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
        for status in ("pending", "approved"):
            ledger.conn.execute(
                "INSERT INTO approval_queue (idempotency_key, run_id, tenant, agent, "
                "action_type, params_json, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"k-{status}", run_id, "demo", "ap", "ap.push", '{"x": 1}', status, "2026-10-04"),
            )
        ledger.conn.commit()
    rows = _tools(books).call("waiting_cards", {})["rows"]
    assert [r["action_type"] for r in rows] == ["ap.push"] and rows[0]["status"] == "pending"


def test_the_tools_never_write(books):
    db = books["root"] / "ledger.sqlite3"
    before = db.stat().st_mtime_ns
    t = _tools(books)
    for name in catalog.TOOL_NAMES:
        t.call(name, {"query": "acme"} if name == "find_invoices" else {})
    assert db.stat().st_mtime_ns == before


def test_an_unknown_tool_is_an_error():
    with pytest.raises(KeyError):
        catalog.Tools("demo", ledger_root=Path("/nonexistent")).call("move_money", {})


# ---- the MCP layer ------------------------------------------------------------


def _rpc(server: McpServer, method: str, params: dict | None = None, mid: int = 1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_initialize_list_and_call(books):
    server = McpServer(_tools(books))
    init = _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert init["result"]["serverInfo"]["name"] == "timothy-engine"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in _rpc(server, "tools/list")["result"]["tools"]}
    assert names == set(catalog.TOOL_NAMES)
    called = _rpc(server, "tools/call", {"name": "open_payables", "arguments": {}})
    payload = json.loads(called["result"]["content"][0]["text"])
    assert payload["rows"][0]["amount"] == "1234.56"
    assert called["result"].get("isError") is not True


def test_errors_are_errors(books):
    server = McpServer(_tools(books))
    assert _rpc(server, "no/such")["error"]["code"] == -32601
    bad = _rpc(server, "tools/call", {"name": "move_money", "arguments": {}})
    assert bad["result"]["isError"] is True
    assert _rpc(server, "ping")["result"] == {}


def test_engine_mcp_speaks_over_stdio(books):
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
            "params": {"name": "find_invoices", "arguments": {"query": "A-100"}},
        },
    ]
    env = {**os.environ, "ENGINE_LEDGER_ROOT": str(books["ledger_dir"])}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.engine.cli",
            "mcp",
            "demo",
            "--obligations-file",
            str(books["obligations"]),
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
    rows = json.loads(replies[1]["result"]["content"][0]["text"])["rows"]
    assert [r["invoice_number"] for r in rows] == ["A-100"]


def test_the_last_sealed_month_is_found_however_many_events_follow(books):
    with Ledger.open(books["root"]) as ledger:
        run_id = ledger.conn.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
        rows = [("close.locked", "2026-08")] + [("close.preflight", "2026-09")] * 15
        for i, (etype, month) in enumerate(rows):
            ledger.conn.execute(
                "INSERT INTO events (idempotency_key, run_id, tenant, agent, event_type, "
                "payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (f"c{i}", run_id, "demo", "close", etype, f'{{"month": "{month}"}}', "2026-10-0"),
            )
        ledger.conn.commit()
    out = _tools(books).call("close_status", {"limit": 3})
    assert out["last_locked_month"] == "2026-08" and len(out["rows"]) == 3
