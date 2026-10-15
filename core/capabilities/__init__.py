"""What Tim can do today, built from the code (Tim's front door, 2026-10-09).

The capability page lists the tools a person can ask (core/tools/catalog.py),
the questions setup asks (core/onboarding/questions.toml), what each
registered lane does on its own, each in one plain sentence from
``capabilities.toml``, and what Tim cannot do yet. Everything but the sentences
comes from the engine itself, and a test fails when a lane has no sentence,
so the page says what the code does.

Every tool the door can offer is on it, the ones that read ("Ask Tim") and
the ones that act ("Tell Tim"), each with a way a person might say it
(2026-10-13: the three that act were missing, and all three "not yet" lines
had shipped). With a shape, setup lists only the questions that shape is
asked. docs/what-tim-can-do.md is this page, checked by a test.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path

from ..engine.registry import list_agents
from ..onboarding import load_questions
from ..tools.catalog import (
    ADD_RECEIPT,
    AGREE_TO_READINGS,
    ASK_THE_RULES,
    CATCH_UP_LINK,
    DECIDE,
    RECEIPTS_TO_READ,
    RECORD_READING,
    SEND_MONTH_FOR_REVIEW,
    SET_AMOUNT,
    SET_CATEGORY,
    SET_DRIVE_PURPOSE,
    SET_RECEIPT_DAY,
    SPECS,
    STATEMENT_TOOLS,
    ToolSpec,
)

DOOR_SPECS: tuple[ToolSpec, ...] = (
    *SPECS,
    DECIDE,
    ADD_RECEIPT,
    SET_CATEGORY,
    SET_AMOUNT,
    SET_DRIVE_PURPOSE,
    CATCH_UP_LINK,
    SET_RECEIPT_DAY,
    RECEIPTS_TO_READ,
    RECORD_READING,
    AGREE_TO_READINGS,
    SEND_MONTH_FOR_REVIEW,
    *STATEMENT_TOOLS,
    ASK_THE_RULES,
)
"""Every tool the door can offer a person: the read tools, then the ones that act."""

DATA_FILE = Path(__file__).resolve().parent / "capabilities.toml"


@cache
def _data() -> dict:
    return tomllib.loads(DATA_FILE.read_text(encoding="utf-8"))


def load_lanes() -> dict[str, dict]:
    return dict(_data().get("lane", {}))


def load_tools() -> dict[str, dict]:
    return dict(_data().get("tool", {}))


def acts(spec: ToolSpec) -> bool:
    """Whether the tool changes anything (its MCP hints say so; none = it only reads)."""
    return bool(spec.annotations) and spec.annotations.get("readOnlyHint") is False


def build(shape: str | None = None) -> dict:
    """The page as data: ask, tell, setup, on_its_own, not_yet. With a
    shape, setup holds only the questions that shape is asked."""
    lanes, tools = load_lanes(), load_tools()

    def line(spec: ToolSpec) -> dict:
        return {"name": spec.name, "says": tools[spec.name]["says"], "try": tools[spec.name]["try"]}

    return {
        "ask": [line(s) for s in DOOR_SPECS if s.name in tools and not acts(s)],
        "tell": [line(s) for s in DOOR_SPECS if s.name in tools and acts(s)],
        "setup": [
            {"ask": q.ask, "help": q.help}
            for q in load_questions()
            if shape is None or q.applies_to(shape)
        ],
        "on_its_own": [
            {"lane": name, "says": lanes[name]["says"]}
            for name in list_agents()
            if name in lanes and lanes[name].get("shown", True)
        ],
        "not_yet": [str(item["says"]) for item in load_not_yet()],
    }


def load_not_yet() -> list[dict]:
    return list(_data().get("not_yet", []))


def markdown(page: dict) -> str:
    lines = ["# What Tim can do", ""]
    lines += [
        "<!-- Built from the code by `engine capabilities`; regenerate it, never edit it. -->",
        "",
    ]
    lines += ["## Ask Tim", ""]
    lines += [f'- {t["says"]} *"{t["try"]}"*' for t in page["ask"]]
    lines += ["", "## Tell Tim", ""]
    lines += [f'- {t["says"]} *"{t["try"]}"*' for t in page["tell"]]
    lines += ["", "## What Tim does on its own", ""]
    lines += [f"- {lane['says']}" for lane in page["on_its_own"]]
    lines += ["", "## What Tim asks when you start", ""]
    lines += [f"{n}. {q['ask']}" for n, q in enumerate(page["setup"], start=1)]
    lines += ["", "## Not yet", ""]
    lines += [f"- {item}" for item in page["not_yet"]]
    return "\n".join(lines) + "\n"


__all__ = ["DOOR_SPECS", "acts", "build", "load_lanes", "load_not_yet", "load_tools", "markdown"]
