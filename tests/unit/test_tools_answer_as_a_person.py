"""The read tools answer as the person asking (Tim walkthrough 1, 2026-10-09).

Grace Fellowship's authority.toml gave Ruth, a missionary, ``view:*@own``:
she sees her own unit and nothing else. The read door never consulted it.
`engine mcp grace-fellowship` served the whole church to whoever held the
link, and when Ruth asked whether her September money had come, the engine
handed back the Bakers' report beside hers. The AI chose not to read it out;
that protection cannot rest on an AI keeping quiet.

The promise under test: given a viewer (a person in authority.toml), every
tool returns only the rows that person may view, totals count only those
rows, and a tool the person may not use at all is not offered. Without a
viewer the tools answer as before (the owner's own local use).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.agents.ap import store
from core.agents.expenses.jobs import REVIEW_CARD
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.onboarding import apply, next_question, plan, record
from core.tools import catalog
from core.tools.viewer import ViewerError, viewer_for

REPO = Path(__file__).resolve().parents[2]

OBLIGATIONS = """
[[obligation]]
id = "visa"
title = "Nepal visa renewal"
who = "Ruth Hollis"
due = 2026-10-20

[[obligation]]
id = "insurance"
title = "Church liability insurance"
who = ""
due = 2026-10-25
"""


@pytest.fixture
def church(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    values = {
        "who": "nonprofit-small",
        "legal_name": "Grace Fellowship",
        "your_name": "Carol Jennings",
        "your_role": "treasurer",
        "people": "Ruth Hollis = missionary\nTom and Jen Baker = missionary\nDon Pruitt = board",
    }
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(plan(answers, "grace"), root=root, run_audit=False)
    ledger_root = resolve_ledger_root("grace")
    with Ledger.open(ledger_root) as ledger:
        for key, person, cents in (("r", "Ruth Hollis", 18640), ("b", "Tom and Jen Baker", 241275)):
            ledger.conn.execute(
                "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
                "total_cents, status, created_at, updated_at) "
                "VALUES (?, 'grace', ?, '2026-09', ?, 'Open', 'now', 'now')",
                (key, person, cents),
            )
        ledger.conn.commit()
        store.insert_invoice(
            ledger,
            tenant="grace",
            vendor="Marion Roofing",
            invoice_number="R-12",
            amount_cents=300000,
            due_date="2026-10-30",
            status="Received",
        )
        ledger.conn.execute(
            "INSERT INTO runs (idempotency_key, tenant, agent, job, status, result_json, "
            "created_at) VALUES ('seed-run', 'grace', 'expenses', 'review', 'ok', '{}', 'now')"
        )
        run_id = ledger.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        ledger.conn.commit()
        for key, person in (("card-r", "Ruth Hollis"), ("card-b", "Tom and Jen Baker")):
            ledger.enqueue_approval(
                idempotency_key=key,
                run_id=run_id,
                tenant="grace",
                agent="expenses",
                action_type=REVIEW_CARD,
                params={"person": person, "total_cents": "1000"},
            )
    ob = tmp_path / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    return {"root": root, "ledger_root": ledger_root, "obligations": ob}


def _tools(church, person: str | None) -> catalog.Tools:
    viewer = viewer_for("grace", person, tenants_root=church["root"]) if person else None
    return catalog.Tools(
        "grace",
        ledger_root=church["ledger_root"],
        obligations_file=church["obligations"],
        today="2026-10-09",
        viewer=viewer,
    )


def test_ruth_sees_her_own_report_and_never_the_bakers(church):
    rows = _tools(church, "ruth-hollis").call("expense_reports", {})["rows"]
    assert [r["person"] for r in rows] == ["Ruth Hollis"]


def test_ruth_sees_none_of_the_churchs_bills_and_the_total_counts_only_what_she_sees(church):
    out = _tools(church, "ruth-hollis").call("open_payables", {})
    assert out["rows"] == [] and out["total"] == "0.00"
    assert _tools(church, "ruth-hollis").call("find_invoices", {"query": "Roofing"})["rows"] == []


def test_ruth_sees_only_the_cards_about_her(church):
    rows = _tools(church, "ruth-hollis").call("waiting_cards", {})["rows"]
    assert [r["params"]["person"] for r in rows] == ["Ruth Hollis"]


def test_ruth_sees_her_own_deadlines_and_not_the_churchs(church):
    rows = _tools(church, "ruth-hollis").call("deadlines", {"days": 60})["rows"]
    assert [r["id"] for r in rows] == ["visa"]


def test_the_books_tools_are_not_offered_to_a_missionary(church):
    tools = _tools(church, "ruth-hollis")
    offered = {s.name for s in tools.specs()}
    assert "close_status" not in offered and "recent_runs" not in offered
    with pytest.raises(KeyError):
        tools.call("close_status", {})


def test_the_treasurer_and_the_board_see_the_whole_church(church):
    for person in ("carol-jennings", "don-pruitt"):
        tools = _tools(church, person)
        people = {r["person"] for r in tools.call("expense_reports", {})["rows"]}
        assert people == {"Ruth Hollis", "Tom and Jen Baker"}
        assert tools.call("open_payables", {})["total"] == "3000.00"
        assert "close_status" in {s.name for s in tools.specs()}


def test_without_a_viewer_the_tools_answer_as_before(church):
    tools = _tools(church, None)
    assert len(tools.call("expense_reports", {})["rows"]) == 2
    assert len(tools.call("deadlines", {"days": 60})["rows"]) == 2


def test_a_viewer_must_be_a_person_in_authority_toml(church):
    with pytest.raises(ViewerError, match="nobody"):
        viewer_for("grace", "nobody", tenants_root=church["root"])
    with pytest.raises(ViewerError, match="agent"):
        viewer_for("grace", "intake", tenants_root=church["root"])


def test_the_viewer_never_widens_what_the_rows_say(church):
    """Filtering drops rows; it never edits them."""
    full = _tools(church, None).call("expense_reports", {})["rows"]
    ruth = _tools(church, "ruth-hollis").call("expense_reports", {})["rows"]
    assert ruth == [r for r in full if r["person"] == "Ruth Hollis"]
    assert json.dumps(ruth)  # still plain JSON


def _serve(church, *extra: str) -> subprocess.CompletedProcess:
    lines = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "expense_reports", "arguments": {}},
        },
    ]
    return subprocess.run(
        [sys.executable, "-m", "core.engine.cli", "mcp", "grace", *extra],
        input="\n".join(json.dumps(x) for x in lines) + "\n",
        capture_output=True,
        text=True,
        cwd=REPO,
        env=dict(os.environ),
        timeout=60,
    )


def test_engine_mcp_as_a_person_answers_as_them_over_stdio(church, monkeypatch):
    proc = _serve(church, "--as", "ruth-hollis")
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines()) if "id" in r}
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert "close_status" not in names, proc.stderr
    rows = json.loads(replies[3]["result"]["content"][0]["text"])["rows"]
    assert [r["person"] for r in rows] == ["Ruth Hollis"]


def test_engine_mcp_as_someone_unknown_refuses_before_serving(church):
    proc = _serve(church, "--as", "nobody")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "nobody" in proc.stderr


def test_no_tool_offered_to_ruth_ever_shows_a_neighbors_data(church):
    """The persona loop's first judge, as a unit test: whatever Ruth asks,
    nothing that names the Bakers reaches her."""
    tools = _tools(church, "ruth-hollis")
    args = {"find_invoices": {"query": "a"}, "deadlines": {"days": 3650}}
    for spec in tools.specs():
        out = json.dumps(tools.call(spec.name, args.get(spec.name, {})))
        assert "Baker" not in out, spec.name
        assert "2412.75" not in out, spec.name
