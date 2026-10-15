"""``engine mcp --today``: a replay of a past day reads due dates from that
day (2026-10-12). Replaying early September's books, the model called a bill
due later that month "overdue": Tim's clock read the real today. Stdio only;
the HTTP door, which people use, always keeps the real clock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.unit.test_engine_tools import books  # noqa: F401  (fixture)


def _mcp(books, *extra):  # noqa: F811
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
            "params": {"name": "deadlines", "arguments": {"days": 60}},
        },
    ]
    env = {**os.environ, "ENGINE_LEDGER_ROOT": str(books["ledger_dir"])}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "core.engine.cli",
            "mcp",
            "demo",
            "--obligations-file",
            str(books["obligations"]),
            *extra,
        ],
        input="\n".join(json.dumps(x) for x in lines) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _days_left(proc):
    reply = json.loads(proc.stdout.splitlines()[-1])
    rows = json.loads(reply["result"]["content"][0]["text"])["rows"]
    return {r["id"]: r["days_left"] for r in rows}


def test_today_sets_the_day_the_deadlines_count_from(books):  # noqa: F811
    proc = _mcp(books, "--today", "2026-10-15")
    assert proc.returncode == 0, proc.stderr
    assert _days_left(proc) == {"permit": 5}
    assert "as of 2026-10-15" in proc.stderr


def test_a_malformed_day_is_refused(books):  # noqa: F811
    proc = _mcp(books, "--today", "15 Oct")
    assert proc.returncode == 2 and "YYYY-MM-DD" in proc.stderr


def test_the_http_door_never_takes_a_pretend_day(books):  # noqa: F811
    proc = _mcp(books, "--http", "--today", "2026-10-15")
    assert proc.returncode == 2 and "stdio" in proc.stderr
