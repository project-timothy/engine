"""Tim's questions and answers are held to the code (the owner, 2026-10-14: "people just ask").

One source, core/capabilities/faq.toml, feeds Tim's own answers (start_here),
the page anyone can share (`engine faq`, docs/tim-faq.md), and the alpha
packet. Every tool the door offers is explained by at least one entry, so a
new tool cannot ship without its answer.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from core.capabilities import DOOR_SPECS
from core.capabilities.faq import faq_for, load_faq, markdown
from core.engine.cli import main

DOC = Path(__file__).resolve().parents[2] / "docs" / "tim-faq.md"
DOOR = {s.name for s in DOOR_SPECS}


def test_every_door_tool_has_an_answer_and_every_answer_names_real_tools():
    covered = {name for e in load_faq() for name in e["covers"]}
    assert DOOR - covered == set(), "a tool with no FAQ entry"
    assert covered - DOOR == set(), "an entry naming no such tool"


def test_each_entry_is_whole_and_ids_are_unique():
    entries = load_faq()
    assert len({e["id"] for e in entries}) == len(entries)
    for e in entries:
        assert e["ask"].strip() and e["answer"].strip(), e["id"]
        date.fromisoformat(e["live"])


def test_always_allow_comes_first():
    """The owner's phone run, 2026-10-14: asking before every tool was too much
    friction to keep using Tim."""
    first = load_faq()[0]
    assert first["id"] == "always-allow" and first["covers"] == []
    assert "Always allow" in first["answer"]


def test_the_plain_words_carry_no_machinery_and_no_em_dash():
    text = json.dumps([[e["ask"], e["answer"], e.get("try", "")] for e in load_faq()])
    for word in ("toml", "ledger", "mcp", "tenant", "qbo", "agent", "card #", "—"):
        assert word not in text.lower(), word


def test_a_person_hears_the_answers_for_the_tools_they_have():
    general = [e["id"] for e in load_faq() if not e["covers"]]
    mine = [e["id"] for e in faq_for({"add_receipt", "month_report"})]
    assert mine[: len(general)] == general
    assert "send-a-receipt" in mine and "bank-statements" not in mine


def test_the_command_prints_markdown_and_json(capsys):
    assert main(["faq"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("# Ask Tim") and "Always allow" in md
    assert main(["faq", "--format", "json"]) == 0
    assert [e["id"] for e in json.loads(capsys.readouterr().out)] == [e["id"] for e in load_faq()]


def test_the_docs_page_is_the_page_the_code_builds():
    """docs/tim-faq.md, for anyone reading on GitHub. Regenerate:
    uv run engine faq > docs/tim-faq.md"""
    assert DOC.read_text(encoding="utf-8") == markdown(load_faq())


def test_the_catch_up_words_name_statements_and_school_lists():
    """The 10/14 phone run: the link's words still said only photos or PDFs."""
    from core.capabilities import load_tools
    from core.tools.catalog import CATCH_UP_LINK

    for words in (CATCH_UP_LINK.description, load_tools()["catch_up_link"]["says"]):
        assert "statements" in words and "school list" in words, words
