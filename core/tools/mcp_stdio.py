"""The engine's tools over MCP (JSON-RPC 2.0, newline-delimited, on stdio).

Standard library only, on purpose: the protocol surface a tool server needs is
four methods, and a new dependency is a one-way door (invariant 9). Anything
written to stdout is a protocol message; logs go to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Protocol

from .catalog import ToolSpec

SERVER_NAME = "timothy-engine"
SERVER_VERSION = "0.1.0"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Read-only tools over this tenant's back-office ledger. Every money value is an exact "
    "decimal string; quote it as given and cite its source. These tools never change "
    "anything; acting on an answer is a person's decision."
)
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


class ToolSet(Protocol):
    """What the server serves: the tenant's read-only catalog, or the
    tenantless onboarding tools (core/onboarding/mcp.py)."""

    def specs(self) -> list[ToolSpec]: ...

    def call(self, name: str, args: dict | None = None) -> dict: ...


class McpServer:
    def __init__(
        self, tools: ToolSet, *, name: str = SERVER_NAME, instructions: str = INSTRUCTIONS
    ) -> None:
        self.tools = tools
        self.name = name
        self.instructions = instructions

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """One request in, one response out; a notification gets no reply."""
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:
            return None  # notifications (initialized, cancelled) need no answer
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                result: dict[str, Any] = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": SERVER_VERSION},
                    "instructions": self.instructions,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": s.name,
                            "description": s.description,
                            "inputSchema": s.schema,
                            "annotations": s.annotations or READ_ONLY,
                        }
                        for s in self.tools.specs()
                    ]
                }
            elif method == "tools/call":
                result = self._call(params)
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as exc:  # a broken request never kills the server
            return _error(mid, -32603, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        try:
            payload = self.tools.call(name, params.get("arguments") or {})
        except (KeyError, ValueError, FileNotFoundError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        text = json.dumps(payload, indent=1, sort_keys=False)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def serve(self, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> int:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                reply: dict | None = _error(None, -32700, f"parse error: {exc}")
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "")
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
        return 0


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
