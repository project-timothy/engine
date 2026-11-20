"""What a tool is (``ToolSpec``) and how a tool in its own module joins the
door (``register``), with the small argument helpers every tool module
shares. Kept apart from core/tools/catalog.py so a tool's module never
imports the catalog: the catalog imports the tool modules, never the other
way, and any import order works.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    fn: Callable[[Any, dict], dict]
    annotations: dict | None = None  # MCP tool hints; None = read-only, closed world
    short: str = ""
    """Tim's own loop reads this in place of ``description`` when set (card C1):
    what the tool does, when to call it, what its arguments mean. The door
    always serves ``description``, word for word."""

    @property
    def title(self) -> str:
        """The name a person sees in Claude's tool list."""
        return self.name.replace("_", " ").capitalize()


ADDED: list[tuple[ToolSpec, Callable[[Any], bool]]] = []
"""Tools that live in their own module and register here, each with when
the door offers it (the 11/15 plan, rule 9: no new logic in this file)."""


def register(spec: ToolSpec, offered: Callable[[Any], bool]) -> ToolSpec:
    """Put a tool from its own module on the door, offered when ``offered(tools)``."""
    ADDED.append((spec, offered))
    return spec


def _int(args: dict, key: str, default: int, lo: int = 1, hi: int = 500) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _schema(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
        "additionalProperties": False,
    }


_LIMIT = {"limit": {"type": "integer", "minimum": 1, "maximum": 500}}
