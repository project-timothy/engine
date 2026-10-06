"""The README describes the repository as it is (issue #4, 2026-10-06).

The README once said Phase 1 had no AP/AR/close logic while 27 jobs shipped,
listed 5 of 18 commands, and left five top-level packages out of its layout.
These tests fail the next time a command or a package lands without a line.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.engine.cli import build_parser

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text()


def _section(heading: str) -> str:
    match = re.search(rf"^## {heading}\n(.*?)(?=^## |\Z)", README, re.M | re.S)
    assert match, f"README has a '## {heading}' section"
    return match.group(1)


def _subcommands() -> set[str]:
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    return set(sub.choices)


def test_every_engine_subcommand_is_in_the_command_table():
    table = _section("Commands")
    named = set(re.findall(r"`(?:uv run )?engine ([a-z-]+)", table))
    assert _subcommands() - named == set(), "add these commands to the README table"
    assert named - _subcommands() == set(), "the README names commands the CLI lacks"


def test_every_top_level_package_is_in_the_layout():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    top = {p.split("/", 1)[0] for p in tracked if "/" in p and not p.startswith(".")}
    layout = _section("Repository layout")
    listed = set(re.findall(r"^([a-z_]+)/", layout, re.M))
    assert top - listed == set(), "add these folders to the README layout"


def test_the_readme_no_longer_calls_itself_phase_1():
    assert "No AP/AR/close business logic yet" not in README


_COUNT_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()


def test_the_intro_names_every_shipped_agent_and_counts_them():
    # The drift gap #410 left (2026-10-06): the agent sentence was prose,
    # unchecked. A new agent now fails here until the README names it.
    from core.engine.registry import list_agents

    intro = README.split("## Setup", 1)[0]
    agents = list_agents()
    assert agents, "the registry lists the shipped agents"
    assert [a for a in agents if f"`{a}`" not in intro] == [], "name these agents in the intro"
    assert f"ships {_COUNT_WORDS[len(agents)]} agents" in intro
