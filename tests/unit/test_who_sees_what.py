"""Who sees what at a sending agency (chunk 9, board item t-agwm-scopes).

Each person sees exactly their part: a team leader the people they look
after, finance every month, a missionary their own unit, and member care
the people in their care and never money. Member care is pastoral: their
reads are the ones an agency most wants on record, so a pack that sets
``log_reads = "always"`` gets a line for every read through the door (who,
which tool, whose rows reached them), never the rows themselves.

Set up from the made-up Harvest Fields pack (tests/fixtures/packs/harvest).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.authority import AuthorityError, parse_policy
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.onboarding import apply, next_question, plan, record
from core.tools import catalog
from core.tools.catalog import READ_LOG
from core.tools.viewer import viewer_for

HARVEST = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "harvest"

PEOPLE = {
    "who": "Our ministry organization",
    "legal_name": "Harvest Fields Mission, Inc.",
    "your_name": "Lydia Brooks",
    "your_role": "finance",
    "people": "Ann Lee = team-leader\nJoel Mensah = team-leader\nRita Cole = regional-director\n"
    "Sam Ortiz = missionary\nMei Tan = missionary\nPriya Das = missionary\n"
    "Ken Moss = compliance\nAbby Stone = member-care",
    "looks_after": "Ann Lee = Sam Ortiz, Mei Tan\nJoel Mensah = Priya Das\n"
    "Rita Cole = Sam Ortiz, Mei Tan, Priya Das\nAbby Stone = Mei Tan, Priya Das",
}

OBLIGATIONS = """
[[obligation]]
id = "visa"
title = "Thailand visa renewal"
who = "Mei Tan"
due = 2026-10-20

[[obligation]]
id = "audit"
title = "Agency audit"
who = ""
due = 2026-10-25
"""

MONEY_TOOLS = {"expense_reports", "month_report", "year_report", "open_payables", "find_invoices"}


@pytest.fixture
def agency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    answers: dict = {"pack": str(HARVEST)}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], PEOPLE.get(q["id"], q.get("default", "")))
    apply(plan(answers, "harvest"), root=root, run_audit=False)
    ledger_root = resolve_ledger_root("harvest")
    with Ledger.open(ledger_root) as ledger:
        for key, person, cents in (("m", "Mei Tan", 42000), ("p", "Priya Das", 31000)):
            ledger.conn.execute(
                "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
                "total_cents, status, created_at, updated_at) "
                "VALUES (?, 'harvest', ?, '2026-09', ?, 'Open', 'now', 'now')",
                (key, person, cents),
            )
        ledger.conn.commit()
    ob = tmp_path / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    return {"root": root, "ledger_root": ledger_root, "obligations": ob}


def _tools(agency, person: str) -> catalog.Tools:
    return catalog.Tools(
        "harvest",
        ledger_root=agency["ledger_root"],
        obligations_file=agency["obligations"],
        today="2026-10-09",
        viewer=viewer_for("harvest", person, tenants_root=agency["root"]),
    )


def _reads(agency) -> list[dict]:
    path = Path(agency["ledger_root"]) / READ_LOG
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _months(tools: catalog.Tools) -> set[str]:
    return {r["person"] for r in tools.call("expense_reports", {})["rows"]}


# ---- who sees what -----------------------------------------------------------------


def test_member_care_is_never_offered_a_money_tool(agency):
    tools = _tools(agency, "abby-stone")
    offered = {spec.name for spec in tools.specs()}
    assert not MONEY_TOOLS & offered
    with pytest.raises(KeyError):
        tools.call("expense_reports", {})


def test_member_care_hears_who_is_in_their_care(agency):
    you = _tools(agency, "abby-stone").call("start_here", {})["you"]
    assert [p["name"] for p in you["sees_people"]] == ["Mei Tan", "Priya Das"]
    assert you["sees_people"][0]["does"] == ["missionary"]
    assert you["looks_over"] == []  # seeing people is not looking over their money


def test_member_care_sees_their_peoples_dates_and_not_the_agencys(agency):
    rows = _tools(agency, "abby-stone").call("deadlines", {"days": 30})["rows"]
    assert [r["title"] for r in rows] == ["Thailand visa renewal"]


def test_a_team_leader_sees_only_the_people_they_look_after(agency):
    assert _months(_tools(agency, "ann-lee")) == {"Mei Tan"}
    assert _months(_tools(agency, "joel-mensah")) == {"Priya Das"}


def test_a_regional_director_sees_the_region_and_finance_sees_every_month(agency):
    assert _months(_tools(agency, "rita-cole")) == {"Mei Tan", "Priya Das"}
    assert _months(_tools(agency, "lydia-brooks")) == {"Mei Tan", "Priya Das"}


def test_a_missionary_sees_their_own_unit_only(agency):
    assert _months(_tools(agency, "mei-tan")) == {"Mei Tan"}
    assert _tools(agency, "sam-ortiz").call("expense_reports", {})["rows"] == []


def test_only_someone_who_sees_people_hears_who_is_in_their_care(agency):
    assert _tools(agency, "mei-tan").call("start_here", {})["you"]["sees_people"] == []


# ---- every read on record ----------------------------------------------------------


def test_every_read_is_logged_with_whose_rows_reached_them(agency):
    _tools(agency, "abby-stone").call("deadlines", {"days": 30})
    _tools(agency, "ann-lee").call("expense_reports", {})
    first, second = _reads(agency)
    assert first["person"] == "abby-stone" and first["tool"] == "deadlines"
    assert first["reached"] == ["mei-tan"]
    assert second["person"] == "ann-lee" and second["reached"] == ["mei-tan"]
    assert first["at"].endswith("+00:00")


def test_the_log_never_carries_the_rows(agency):
    _tools(agency, "lydia-brooks").call("expense_reports", {})
    line = json.dumps(_reads(agency))
    assert "420.00" not in line and "42000" not in line and "Thailand" not in line


def test_a_tool_nobody_offered_them_is_logged_as_refused(agency):
    with pytest.raises(KeyError):
        _tools(agency, "abby-stone").call("month_report", {"month": "2026-09"})
    (only,) = _reads(agency)
    assert only["tool"] == "month_report" and only["refused"] is True
    assert only["reached"] == []


def test_a_call_that_errors_is_still_on_record_as_refused(agency, monkeypatch):
    def refuses(_t, _args):
        raise ValueError("Tim can't approve this for you.")

    tools = _tools(agency, "abby-stone")
    spec = tools._specs["deadlines"]
    monkeypatch.setitem(
        tools._specs,
        "deadlines",
        catalog.ToolSpec(spec.name, spec.description, spec.schema, refuses),
    )
    with pytest.raises(ValueError):
        tools.call("deadlines", {})
    (only,) = _reads(agency)
    assert only["tool"] == "deadlines" and only["refused"] is True


def test_a_read_that_cannot_be_logged_is_not_answered(agency, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(catalog, "append_event_line", broken)
    with pytest.raises(OSError):
        _tools(agency, "abby-stone").call("deadlines", {"days": 30})


def test_a_ministry_that_does_not_ask_for_it_keeps_no_read_log(agency):
    tenant = Path(agency["root"]) / "harvest" / "authority.toml"
    text = tenant.read_text(encoding="utf-8").replace('log_reads = "always"\n', "")
    tenant.write_text(text, encoding="utf-8")
    _tools(agency, "ann-lee").call("expense_reports", {})
    assert _reads(agency) == []


def test_log_reads_takes_always_or_never():
    base = {"money": {"out": "human"}}
    assert parse_policy(base).safeguards.log_reads == "never"
    assert parse_policy({**base, "safeguards": {"log_reads": "always"}}).safeguards.log_reads == (
        "always"
    )
    with pytest.raises(AuthorityError):
        parse_policy({**base, "safeguards": {"log_reads": "sometimes"}})
