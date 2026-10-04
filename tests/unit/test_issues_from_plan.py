"""The plan-to-issues script parses the six-field table shape and labels rows deterministically."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lane" / "issues_from_plan.py"
spec = importlib.util.spec_from_file_location("issues_from_plan", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["issues_from_plan"] = mod  # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(mod)

PLAN = """# Phase 9 plan: example

## A. Group

| Id | Title | Goal | Acceptance | Touches | Size | Depends | Door |
|---|---|---|---|---|---|---|---|
| 9.1 | First thing | It works | A test fails then passes | core/x.py | S | none | two-way |
| 9.2 | Second | Works too | Test two | core/y.py | L (note first) | 9.1 | one-way: contract |

Prose between tables is ignored.

| Id | Title | Goal | Acceptance | Touches | Size | Depends | Door |
|---|---|---|---|---|---|---|---|
| 9.3 | Third | Goal | Acc | docs | M | none | two-way |
"""


def test_parses_every_table_and_keeps_order():
    title, rows = mod.parse_plan(PLAN)
    assert title == "Phase 9 plan: example"
    assert [r.id for r in rows] == ["9.1", "9.2", "9.3"]
    assert rows[1].acceptance == "Test two"


def test_labels_follow_size_depends_and_door():
    _, rows = mod.parse_plan(PLAN)
    assert rows[0].labels == ["phase-9", "size-S", "ready"]
    assert rows[1].labels == ["phase-9", "size-L", "one-way-door", "blocked"]
    assert rows[2].labels == ["phase-9", "size-M", "ready"]


def test_issue_title_and_body_carry_the_six_fields():
    _, rows = mod.parse_plan(PLAN)
    assert rows[0].issue_title == "[9.1] First thing"
    body = rows[0].body(epic=42)
    for heading in ("## Goal", "## Acceptance", "## Touches", "## Size", "## Depends", "## Door"):
        assert heading in body
    assert body.startswith("Part of #42")


def test_wrong_header_or_duplicate_id_is_refused():
    with pytest.raises(ValueError):
        mod.parse_plan(PLAN.replace("| Id | Title | Goal |", "| Id | Name | Goal |", 1))
    with pytest.raises(ValueError):
        mod.parse_plan(PLAN.replace("| 9.3 |", "| 9.1 |"))
