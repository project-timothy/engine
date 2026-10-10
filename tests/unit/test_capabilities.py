"""The capability page is built from the code, so it cannot drift (Tim's front door, 2026-10-09).

The owner, 2026-10-09: "we're building a lot of code, but I honestly can't see what Tim
does." `engine capabilities` answers that from the engine itself: the tools a
person can ask, the questions setup asks, and what each lane does on its
own, plus an honest list of what Tim cannot do yet.
"""

from __future__ import annotations

import json

from core.capabilities import build, load_lanes, load_tools
from core.engine.cli import main
from core.engine.registry import list_agents
from core.onboarding import load_questions
from core.tools.catalog import SPECS


def test_every_registered_lane_has_a_plain_sentence_and_no_sentence_is_orphaned():
    assert set(load_lanes()) == set(list_agents())


def test_every_tool_has_a_plain_sentence_and_no_sentence_is_orphaned():
    assert set(load_tools()) == {s.name for s in SPECS}


def test_the_page_names_every_tool_every_question_and_every_shown_lane():
    page = build()
    assert {t["name"] for t in page["ask"]} == {s.name for s in SPECS}
    assert [q["ask"] for q in page["setup"]] == [q.ask for q in load_questions()]
    shown = {name for name, lane in load_lanes().items() if lane.get("shown", True)}
    assert {lane["lane"] for lane in page["on_its_own"]} == shown
    assert page["not_yet"]


def test_the_plain_words_carry_no_machinery():
    text = json.dumps(
        [lane["says"] for lane in build()["on_its_own"]]
        + [tool["says"] for tool in build()["ask"]]
        + build()["not_yet"]
    ).lower()
    for word in ("toml", "ledger", "scope", "mcp", "tenant", "qbo", "agent"):
        assert word not in text, word


def test_the_command_prints_markdown_and_json(capsys):
    assert main(["capabilities"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("# What Tim can do") and "## Not yet" in md
    assert main(["capabilities", "--format", "json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"ask", "setup", "on_its_own", "not_yet"}
