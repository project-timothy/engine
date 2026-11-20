"""Tim's questions and answers, one source for every place a person reads them.

The owner, 2026-10-14: "people just ask." ``faq.toml`` holds each question
the way a person would put it, a short plain answer, a way to say it, and the
door tools it explains. It feeds Tim's own answers (the start_here tool
hands a person the entries for the tools they have), the page anyone can
share (``engine faq``, docs/tim-faq.md), and the alpha packet. A test holds
it to the code: every door tool is explained by at least one entry.

This module imports nothing from core/tools, so the catalog can read it.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path

FAQ_FILE = Path(__file__).resolve().parent / "faq.toml"


@cache
def _entries() -> tuple[dict, ...]:
    data = tomllib.loads(FAQ_FILE.read_text(encoding="utf-8"))
    return tuple(
        {
            "id": str(e["id"]),
            "ask": str(e["ask"]),
            "answer": str(e["answer"]),
            "try": str(e.get("try", "")),
            "covers": [str(name) for name in e.get("covers", [])],
            "live": str(e["live"]),
            "rules": bool(e.get("rules", False)),
            "voice": str(e.get("voice", "")),
        }
        for e in data.get("entry", [])
    )


VOICES = ("", "on", "off")
"""An entry's ``voice``: "" holds everywhere; "on" only where Tim's own
voice is on (the ministry's file names a tier for ``talk``), "off" only
where it isn't. One id may have one of each (card T2, 2026-11-17: where his
voice is on, Tim's own page comes first and the AI is an "also")."""


def load_faq(*, voice: bool = False) -> list[dict]:
    """Every entry for a ministry whose voice is ``voice``, in page order.
    The ``voice`` field stays out, so a voice-off answer is the one it was."""
    drop = "off" if voice else "on"
    return [{k: v for k, v in e.items() if k != "voice"} for e in _entries() if e["voice"] != drop]


RULES_TOOL = "ask_the_rules"
"""The door offers it only where the ministry has handbook rules."""


def faq_for(offered: Iterable[str], *, voice: bool = False) -> list[dict]:
    """The entries for a person with these tools: the ones for everyone,
    then each one that explains a tool they have, in page order. An entry
    about handbook rules only where the door offers them; an entry about
    Tim's own voice by whether it's on."""
    have = set(offered)
    return [
        e
        for e in load_faq(voice=voice)
        if (not e["covers"] or have & set(e["covers"])) and (not e["rules"] or RULES_TOOL in have)
    ]


def markdown(entries: list[dict]) -> str:
    lines = ["# Ask Tim", ""]
    lines += [
        "<!-- Built from core/capabilities/faq.toml by `engine faq`; regenerate it, "
        "never edit it. -->",
        "",
        "Questions people ask about Tim, with short answers. You can also just ask Tim.",
    ]
    for e in entries:
        lines += ["", f"## {e['ask']}", "", e["answer"]]
        if e["try"]:
            lines += ["", f'Try: *"{e["try"]}"*']
    return "\n".join(lines) + "\n"


__all__ = ["FAQ_FILE", "RULES_TOOL", "VOICES", "faq_for", "load_faq", "markdown"]
