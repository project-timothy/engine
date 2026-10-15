"""The capability page is built from the code, so it cannot drift (Tim's front door, 2026-10-09).

The owner, 2026-10-09: "we're building a lot of code, but I honestly can't see what Tim
does." `engine capabilities` answers that from the engine itself: the tools a
person can ask, the questions setup asks, and what each lane does on its
own, plus an honest list of what Tim cannot do yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.capabilities import DOOR_SPECS, acts, build, load_lanes, load_tools, markdown
from core.engine.cli import main
from core.engine.registry import list_agents
from core.onboarding import SHAPES, load_questions
from core.tools.catalog import SPECS

DOC = Path(__file__).resolve().parents[2] / "docs" / "what-tim-can-do.md"


def test_every_registered_lane_has_a_plain_sentence_and_no_sentence_is_orphaned():
    assert set(load_lanes()) == set(list_agents())


def test_every_tool_has_a_plain_sentence_and_no_sentence_is_orphaned():
    """Every tool the door can offer, the ones that act too (they were
    missing until 2026-10-13), with a way a person might say it."""
    assert set(load_tools()) == {s.name for s in DOOR_SPECS}
    assert {s.name for s in SPECS} < {s.name for s in DOOR_SPECS}
    assert all(tool.get("try") for tool in load_tools().values())


def test_the_page_names_every_tool_every_question_and_every_shown_lane():
    page = build()
    assert {t["name"] for t in page["ask"]} == {s.name for s in DOOR_SPECS if not acts(s)}
    assert {t["name"] for t in page["tell"]} == {
        "decide_card",
        "add_receipt",
        "set_category",
        "set_amount",
        "set_drive_purpose",
        "catch_up_link",
        "set_receipt_day",
        "record_reading",
        "agree_to_readings",
        "send_month_for_review",
        "bank_statement",
        "keep_own_lines",
        "keep_school_list",
        "explain_bank_line",
    }
    assert [q["ask"] for q in page["setup"]] == [q.ask for q in load_questions()]
    shown = {name for name, lane in load_lanes().items() if lane.get("shown", True)}
    assert {lane["lane"] for lane in page["on_its_own"]} == shown
    assert page["not_yet"]


def test_the_plain_words_carry_no_machinery():
    text = json.dumps(
        [lane["says"] for lane in build()["on_its_own"]]
        + [tool["says"] for tool in build()["ask"] + build()["tell"]]
        + build()["not_yet"]
    ).lower()
    for word in ("toml", "ledger", "scope", "mcp", "tenant", "qbo", "agent"):
        assert word not in text, word


def test_the_command_prints_markdown_and_json(capsys):
    assert main(["capabilities"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("# What Tim can do") and "## Not yet" in md
    assert main(["capabilities", "--format", "json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {
        "ask",
        "tell",
        "setup",
        "on_its_own",
        "not_yet",
    }


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_shape_hears_only_its_own_setup_questions(shape):
    asked = [q["ask"] for q in build(shape)["setup"]]
    assert asked == [q.ask for q in load_questions() if q.applies_to(shape)]


def test_a_missionary_is_not_asked_about_business_taxes(capsys):
    assert main(["capabilities", "--format", "json", "--shape", "nonprofit-solo"]) == 0
    asked = " ".join(q["ask"] for q in json.loads(capsys.readouterr().out)["setup"])
    assert "another kind of charity" in asked and "business set up for taxes" not in asked


def test_the_docs_page_is_the_page_the_code_builds():
    """docs/what-tim-can-do.md, for anyone reading on GitHub. Regenerate:
    uv run engine capabilities > docs/what-tim-can-do.md"""
    assert DOC.read_text(encoding="utf-8") == markdown(build())
