"""What's waiting reads in the person's terms, wherever they ask (2026-10-11).

The variance run's last misses were the model thinking aloud: "Card 1 is
Ruth's report..." before it acted, and "the expense reports list came back
empty" when Carol asked about Ruth's September report. Both start in what
the tools hand the model, and the Claude app drops the server's
instructions, so the fix lives in the tool text and the tool replies:

1. Each waiting card carries ``about``: whose, what for, and how much, the
   words the Approve page uses. The card number is for ``decide_card`` only.
2. A report waiting for a yes has no ``expense_report`` row yet (the row is
   written once the review card is approved), so ``expense_reports`` lists
   it from its card, "Waiting for approval", instead of coming back empty.
"""

from __future__ import annotations

import json

import pytest

from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.tools import catalog


@pytest.fixture
def books(tmp_path):
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        c = ledger.conn
        c.execute(
            "INSERT INTO runs (idempotency_key, tenant, agent, job, status, result_json, "
            "created_at) VALUES ('r1', 'demo', 'expenses', 'report', 'ok', '{}', 'now')"
        )
        run_id = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        for key, person, cents, status in (
            ("c-ruth", "Ruth Hollis", "68640", "pending"),
            ("c-old", "Don Pruitt", "5400", "approved"),
        ):
            c.execute(
                "INSERT INTO approval_queue (idempotency_key, run_id, tenant, agent, "
                "action_type, params_json, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    key,
                    run_id,
                    "demo",
                    "expenses",
                    "expenses.report_review",
                    json.dumps({"person": person, "month": "2026-09", "total_cents": cents}),
                    status,
                    "now",
                ),
            )
        c.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month, total_cents, "
            "status, created_at, updated_at) VALUES "
            "('e-don', 'demo', 'Don Pruitt', '2026-09', 5400, 'Open', 'now', 'now')"
        )
        c.commit()
    return root


def _tools(root):
    return catalog.Tools("demo", ledger_root=root, today="2026-10-11")


def test_a_waiting_card_says_whose_what_and_how_much(books):
    rows = _tools(books).call("waiting_cards", {})["rows"]
    assert [r["about"] for r in rows] == [
        "an expense report for Ruth Hollis, September 2026, $686.40"
    ]


def test_a_report_waiting_for_a_yes_is_listed_with_the_reports(books):
    rows = _tools(books).call("expense_reports", {})["rows"]
    assert [(r["person"], r["month"], r["total"], r["status"]) for r in rows] == [
        ("Ruth Hollis", "2026-09", "686.40", "Waiting for approval"),
        ("Don Pruitt", "2026-09", "54.00", "Open"),
    ]
    assert rows[0]["source"].startswith("approval_queue#")


def test_the_limit_holds_across_both_kinds(books):
    rows = _tools(books).call("expense_reports", {"limit": 1})["rows"]
    assert [r["person"] for r in rows] == ["Ruth Hollis"]


def test_the_tool_text_points_the_model_at_the_plain_words():
    specs = {s.name: s.description for s in catalog.SPECS}
    assert "about" in specs["waiting_cards"]
    assert "decide_card" in specs["waiting_cards"]
    assert "waiting for approval" in specs["expense_reports"].lower()
