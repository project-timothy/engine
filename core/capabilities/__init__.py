"""What Tim can do today, built from the code (Tim's front door, 2026-10-09).

The capability page lists the tools a person can ask (core/tools/catalog.py),
the questions setup asks (core/onboarding/questions.toml), what each
registered lane does on its own, each in one plain sentence from
``capabilities.toml``, and what Tim cannot do yet. Everything but the sentences
comes from the engine itself, and a test fails when a lane has no sentence,
so the page says what the code does.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path

from ..engine.registry import list_agents
from ..onboarding import load_questions
from ..tools.catalog import SPECS

DATA_FILE = Path(__file__).resolve().parent / "capabilities.toml"


@cache
def _data() -> dict:
    return tomllib.loads(DATA_FILE.read_text(encoding="utf-8"))


def load_lanes() -> dict[str, dict]:
    return dict(_data().get("lane", {}))


def load_tools() -> dict[str, dict]:
    return dict(_data().get("tool", {}))


def build() -> dict:
    """The page as data: ask, setup, on_its_own, not_yet."""
    lanes, tools = load_lanes(), load_tools()
    return {
        "ask": [{"name": s.name, "says": tools[s.name]["says"]} for s in SPECS if s.name in tools],
        "setup": [{"ask": q.ask, "help": q.help} for q in load_questions()],
        "on_its_own": [
            {"lane": name, "says": lanes[name]["says"]}
            for name in list_agents()
            if name in lanes and lanes[name].get("shown", True)
        ],
        "not_yet": [str(item["says"]) for item in _data().get("not_yet", [])],
    }


def markdown(page: dict) -> str:
    lines = ["# What Tim can do", ""]
    lines += ["## Ask Tim", ""]
    lines += [f"- {t['says']}" for t in page["ask"]]
    lines += ["", "## What Tim does on its own", ""]
    lines += [f"- {lane['says']}" for lane in page["on_its_own"]]
    lines += ["", "## What Tim asks when you start", ""]
    lines += [f"{n}. {q['ask']}" for n, q in enumerate(page["setup"], start=1)]
    lines += ["", "## Not yet", ""]
    lines += [f"- {item}" for item in page["not_yet"]]
    return "\n".join(lines) + "\n"


__all__ = ["build", "load_lanes", "load_tools", "markdown"]
