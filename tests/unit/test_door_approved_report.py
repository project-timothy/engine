"""A report someone just approved is still a report (2026-10-12).

The review card's yes is recorded at once, but the ``expense_report`` row is
written by the next expenses run (08:00 in production; never, on a practice
box). Between the two, ``expense_reports`` showed neither the waiting card
nor a filed row, so GPT-5.5 told Carol "I don't see any expense reports"
an hour after she approved Ruth's. Until its row exists, an approved report
is listed from its card as "Approved".
"""

from __future__ import annotations

import json

import pytest

from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.tools import catalog


def _card(c, run_id, key, person, month, cents, status):
    c.execute(
        "INSERT INTO approval_queue (idempotency_key, run_id, tenant, agent, action_type, "
        "params_json, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            key,
            run_id,
            "demo",
            "expenses",
            "expenses.report_review",
            json.dumps({"person": person, "month": month, "total_cents": cents}),
            status,
            "now",
        ),
    )


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
        # Ruth's yes is in; the run that files it has not come yet.
        _card(c, run_id, "c-ruth", "Ruth Hollis", "2026-09", "68640", "approved")
        # Don's yes came and his report is filed: one row, never two.
        _card(c, run_id, "c-don", "Don Pruitt", "2026-09", "5400", "approved")
        c.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month, total_cents, "
            "status, created_at, updated_at) VALUES "
            "('e-don', 'demo', 'Don Pruitt', '2026-09', 5400, 'Open', 'now', 'now')"
        )
        # Ann's August report was approved, then a receipt came in and a new
        # card waits: the waiting card speaks for that month.
        _card(c, run_id, "c-ann-1", "Ann Lee", "2026-08", "1000", "approved")
        _card(c, run_id, "c-ann-2", "Ann Lee", "2026-08", "1500", "pending")
        # A rejected card is not a report.
        _card(c, run_id, "c-bob", "Bob Ames", "2026-09", "700", "rejected")
        c.commit()
    return root


def _rows(root, **args):
    tools = catalog.Tools("demo", ledger_root=root, today="2026-10-12")
    return tools.call("expense_reports", args)["rows"]


def test_an_approved_report_not_yet_filed_is_listed_as_approved(books):
    ruth = [r for r in _rows(books) if r["person"] == "Ruth Hollis"]
    assert [(r["month"], r["total"], r["status"]) for r in ruth] == [
        ("2026-09", "686.40", "Approved")
    ]
    assert ruth[0]["source"].startswith("approval_queue#")


def test_a_filed_report_is_listed_once(books):
    don = [r for r in _rows(books) if r["person"] == "Don Pruitt"]
    assert [(r["status"], r["source"].split("#")[0]) for r in don] == [("Open", "expense_report")]


def test_a_newer_waiting_card_speaks_for_its_month(books):
    ann = [r for r in _rows(books) if r["person"] == "Ann Lee"]
    assert [(r["total"], r["status"]) for r in ann] == [("15.00", "Waiting for approval")]


def test_a_rejected_card_is_not_a_report(books):
    assert "Bob Ames" not in {r["person"] for r in _rows(books)}


def test_the_tool_text_names_approved():
    specs = {s.name: s.description for s in catalog.SPECS}
    assert "approved" in specs["expense_reports"].lower()


def test_the_tool_text_sends_a_yes_to_decide_card():
    # Variance 2026-10-12, GPT-5.4-mini, engine #486: "Approve Ruth's report"
    # went to expense_reports, which says "approved", and stopped at "it's
    # waiting for approval" (14/20 on #485, 9/20 on #486).
    specs = {s.name: s.description for s in catalog.SPECS}
    assert "to approve or decline one" in specs["expense_reports"].lower()
    assert "decide_card" in specs["expense_reports"]
