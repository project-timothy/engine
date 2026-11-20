"""Tim's questions and answers are held to the code (the owner, 2026-10-14: "people just ask").

One source, core/capabilities/faq.toml, feeds Tim's own answers (start_here),
the page anyone can share (`engine faq`, docs/tim-faq.md), and the alpha
packet. Every tool the door offers is explained by at least one entry, so a
new tool cannot ship without its answer.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from core.capabilities import DOOR_SPECS
from core.capabilities.faq import faq_for, load_faq, markdown
from core.engine.cli import main
from tests.unit.test_door_my_words import no_plan, solo  # noqa: F401  (fixture)

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


RULES_ONLY = {"rules-after", "rules-before", "ask-first", "handbook-changes"}


def test_the_handbook_answers_reach_only_a_door_with_rules():
    """Caleb's guide (the dress rehearsal, 2026-10-15): a ministry with no
    handbook rules heard how Tim checks a month against them."""
    plain = {e["id"] for e in faq_for({"month_report", "decide_card"})}
    assert not plain & RULES_ONLY
    ruled = {
        e["id"] for e in faq_for({"month_report", "decide_card", "ask_the_rules", "ask_for_ok"})
    }
    assert RULES_ONLY <= ruled


def test_the_answers_for_everyone_fit_every_kind_of_ministry():
    for e in load_faq():
        if not e["covers"]:
            assert "church" not in (e["ask"] + e["answer"]).lower(), e["id"]
    bills = next(e for e in load_faq() if e["id"] == "bills")
    assert "church" not in bills["ask"].lower()


def test_a_ministrys_page_holds_only_what_its_door_offers(solo, capsys):  # noqa: F811
    no_plan(solo)
    assert main(["faq", "--format", "json", "--tenant", "living-water"]) == 0
    ids = {e["id"] for e in json.loads(capsys.readouterr().out)}
    assert "send-a-receipt" in ids and "always-allow" in ids
    assert not ids & RULES_ONLY


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


def test_claude_steps_say_customize_and_each_box_is_its_own_connector():
    """The owner, 2026-11-12: Claude moved connectors under Customize, and one
    Claude account holds one connector per ministry's box. ChatGPT still says
    Settings, so only Claude's steps are held to it."""
    for e in load_faq():
        assert "Settings, Connectors" not in e["answer"], e["id"]
        assert "Settings, then Connectors" not in e["answer"], e["id"]
    entries = {e["id"]: e for e in load_faq()}
    assert "Customize, then Connectors" in entries["always-allow"]["answer"]
    assert "its own connector" in entries["two-ministries"]["answer"]


# "Card" means the card she pays with, and only that (the owner's read of
# 2026-11-19: the inside word for something waiting on a yes read as a credit
# card). Every "card" in what she reads sits in one of these.
_PAY_CARD = re.compile(
    r"\b(?:ministry's|your|my|the) card\b(?:'s)?(?! (?:says|shows|waits|of its own))"
    r"|\bcard purchases\b|\bby card\b|\bcard's (?:dollars|line)\b|\bcard paid\b"
    r"|\bwas the card\b",
)


def test_card_in_her_words_is_always_the_card_she_pays_with():
    stray = []
    for entry in load_faq() + load_faq(voice=True):
        for field in ("ask", "answer", "try"):
            text = str(entry.get(field) or "")
            leftover = _PAY_CARD.sub("", text)
            if re.search(r"\bcards?\b", leftover, re.IGNORECASE):
                stray.append((entry["id"], field))
    assert stray == []
