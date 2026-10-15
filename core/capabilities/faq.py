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
        }
        for e in data.get("entry", [])
    )


def load_faq() -> list[dict]:
    """Every entry, in page order."""
    return [dict(e) for e in _entries()]


def faq_for(offered: Iterable[str]) -> list[dict]:
    """The entries for a person with these tools: the ones for everyone,
    then each one that explains a tool they have, in page order."""
    have = set(offered)
    return [e for e in load_faq() if not e["covers"] or have & set(e["covers"])]


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


__all__ = ["FAQ_FILE", "faq_for", "load_faq", "markdown"]
