"""Authority wired into the approval queue (#435; docs/tenant-kit-design.md,
section 3).

Once a tenant has ``authority.toml`` it is the only source: who may decide a
card, which card types only a person decides, and what a lane may do with no
one asked. Card decisions name a principal (``--as``); a person decides only
at a terminal, a headless caller only as an agent; every decision goes
through ``core.authority.evaluate`` and records who decided and why. The
brief's send and the deadlines calendar write ask the evaluator in place of
their ``unattended`` lists, and an allowed one still leaves a card, decided
by the lane's agent. A tenant with no ``authority.toml`` is untouched
(``test_authority_equivalence.py``).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from core.agents.ap import store
from core.agents.brief import jobs as brief_jobs
from core.agents.deadlines import calendar_sync as cs
from core.authority import AuthorityError, CardRule, parse_policy
from core.engine import cli
from core.engine.cli import main
from core.engine.registry import load_card_authority
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from tests.unit.authority_equivalence import OBLIGATIONS, FakeCalendar, FakeMailer

REPO = Path(__file__).resolve().parents[2]
LANDING = REPO / "core/agents/ap/evals/fixtures/landing"

POLICY = """
[money]
out = "human"

[safeguards]
self_approval = "never"
distinct_payer = "always"
second_approver_above = 5000
authority_confirm = "second_person"
report_self_approvals = "always"

[roles.owner]
grants = ["*:*"]

[roles.approver]
grants = ["view:*", "approve:*"]

[roles.agent-ap]
grants = ["view:*", "submit:ap.invoice", "approve:ap.invoice<=1000"]

[roles.agent-courier]
grants = ["view:*", "send:message", "send:calendar"]

[roles.agent-quiet]
grants = ["view:*"]

[agents.intake]
roles = ["agent-ap"]
reads_outside = true
lanes = ["ap"]

[agents.courier]
roles = ["agent-courier"]
reads_outside = false
lanes = ["brief", "deadlines"]

[people.dana]
roles = ["owner"]

[people.sam]
roles = ["approver"]

[people.jordan-hale]
roles = ["approver"]
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "tenants"
    shutil.copytree(REPO / "tenants" / "demo", root / "demo")
    (root / "demo" / "authority.toml").write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    return {"tmp": tmp_path, "root": root, "data": tmp_path / "data"}


@pytest.fixture
def at_terminal(monkeypatch):
    def _set(present: bool, typed: str = "") -> None:
        monkeypatch.setattr(cli, "_operator_at_terminal", lambda: present)
        monkeypatch.setattr("builtins.input", lambda prompt="": typed)

    _set(False)
    return _set


def _policy(world, text: str) -> None:
    (world["root"] / "demo" / "authority.toml").write_text(text, encoding="utf-8")


def _seed(world) -> None:
    code = main(
        [
            "run", "demo", "ap", "intake", "--shadow", "--ledger-dir", str(world["data"]),
            "--param", f"landing_dir={LANDING}", "--param", "extractor=fixture",
        ]
    )  # fmt: skip
    assert code == 0


def _cards(world) -> list[dict]:
    with Ledger.open(resolve_ledger_root("demo", world["data"])) as ledger:
        return ledger.list_approvals("demo")


def _card(world, action_type: str) -> dict:
    (row,) = [r for r in _cards(world) if r["action_type"] == action_type]
    return row


def _enqueue(world, action_type: str, params: dict, agent: str = "ap") -> dict:
    """A card of any type, hung on a real run (the seed's)."""
    if not _cards(world):
        _seed(world)
    with Ledger.open(resolve_ledger_root("demo", world["data"])) as ledger:
        run_id = ledger.conn.execute("SELECT MIN(id) FROM runs").fetchone()[0]
        ledger.enqueue_approval(
            idempotency_key=f"test:{action_type}:{json.dumps(params, sort_keys=True)}",
            run_id=run_id,
            tenant="demo",
            agent=agent,
            action_type=action_type,
            params=params,
        )
    return _card(world, action_type)


def _decide(world, verb: str, card_id: int, *extra: str) -> int:
    return main(
        ["queue", verb, "demo", "--id", str(card_id), "--ledger-dir", str(world["data"]), *extra]
    )


# ---- who is deciding -------------------------------------------------------------


def test_a_decision_must_name_who_is_deciding(world, at_terminal, capsys):
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "approve", card["id"]) == 2
    assert "--as" in capsys.readouterr().err
    assert _card(world, "ap.review_revised_invoice")["status"] == "pending"


def test_a_person_decides_only_at_a_terminal(world, at_terminal, capsys):
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "approve", card["id"], "--as", "dana") == 2
    assert "terminal" in capsys.readouterr().err
    at_terminal(True)
    assert _decide(world, "approve", card["id"], "--as", "dana") == 0
    params = _card(world, "ap.review_revised_invoice")["params"]
    assert params["decided_by"] == "dana"
    assert "granted by" in params["authority_reason"]


def test_an_agent_decides_within_its_grant_headless(world, at_terminal):
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")  # new_amount 475.00, under its 1000
    assert _decide(world, "approve", card["id"], "--as", "intake") == 0
    row = _card(world, "ap.review_revised_invoice")
    assert row["status"] == "approved"
    assert row["params"]["decided_by"] == "intake"


def test_an_agent_over_its_limit_is_refused(world, at_terminal, capsys):
    _policy(world, POLICY.replace("approve:ap.invoice<=1000", "approve:ap.invoice<=100"))
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "approve", card["id"], "--as", "intake") == 2
    assert "no grant" in capsys.readouterr().err
    assert _card(world, "ap.review_revised_invoice")["status"] == "pending"


def test_an_agent_without_a_grant_is_refused_and_nothing_changes(world, at_terminal, capsys):
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "reject", card["id"], "--as", "courier") == 2
    assert "no grant" in capsys.readouterr().err
    row = _card(world, "ap.review_revised_invoice")
    assert row["status"] == "pending"
    assert "decided_by" not in row["params"]


def test_a_headless_caller_cannot_claim_to_be_a_person(world, at_terminal, capsys):
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "approve", card["id"], "--as", "sam") == 2
    assert "terminal" in capsys.readouterr().err


def test_an_unknown_principal_is_refused(world, at_terminal, capsys):
    _seed(world)
    at_terminal(True)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "approve", card["id"], "--as", "mallory") == 2
    assert "mallory" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["decided_by", "authority_reason"])
def test_no_one_writes_the_decision_stamp_by_hand(world, at_terminal, field):
    _seed(world)
    at_terminal(True)
    card = _card(world, "ap.review_revised_invoice")
    code = _decide(world, "approve", card["id"], "--as", "dana", "--param", f"{field}=dana")
    assert code == 2
    assert _card(world, "ap.review_revised_invoice")["status"] == "pending"


def test_as_means_nothing_without_an_authority_file(world, at_terminal, capsys):
    (world["root"] / "demo" / "authority.toml").unlink()
    _seed(world)
    card = _card(world, "ap.review_revised_invoice")
    assert _decide(world, "approve", card["id"], "--as", "intake") == 2
    assert "authority.toml" in capsys.readouterr().err


# ---- the new-vendor door and the second approver ---------------------------------------


def test_no_agent_grant_reaches_the_new_vendor_door(world, at_terminal, capsys):
    _policy(world, POLICY.replace('"approve:ap.invoice<=1000"', '"approve:ap.invoice"'))
    _seed(world)
    card = _card(world, "ap.new_vendor_decision")
    assert _decide(world, "approve", card["id"], "--as", "intake") == 2
    assert "no grant" in capsys.readouterr().err


def test_a_person_still_types_a_human_only_card_back(world, at_terminal):
    _seed(world)
    card = _card(world, "ap.new_vendor_decision")
    at_terminal(True, typed="y")
    assert _decide(world, "approve", card["id"], "--as", "dana") == 2
    at_terminal(True, typed=str(card["id"]))
    assert _decide(world, "approve", card["id"], "--as", "dana") == 0
    params = _card(world, "ap.new_vendor_decision")["params"]
    assert params["decided_via"] == "terminal"
    assert params["decided_by"] == "dana"


def test_past_the_second_approver_line_it_takes_two_people(world, at_terminal, capsys):
    """#435 refused the one yes; #436 records it and waits for a second
    distinct approver on the same card."""
    card = _enqueue(
        world,
        "ap.payment_recommendation",
        {"payee": "Acme", "invoice_ref": "A-1", "amount": "6000.00"},
    )
    at_terminal(True)
    assert _decide(world, "approve", card["id"], "--as", "sam") == 0
    assert "waiting on" in capsys.readouterr().out
    assert _card(world, "ap.payment_recommendation")["status"] == "pending"
    assert _decide(world, "approve", card["id"], "--as", "sam") == 2
    assert "already approved" in capsys.readouterr().err
    assert _decide(world, "approve", card["id"], "--as", "dana") == 0
    row = _card(world, "ap.payment_recommendation")
    assert row["status"] == "approved"
    assert row["params"]["approvers"] == "sam, dana"


def test_rejecting_needs_no_second_approver(world, at_terminal):
    card = _enqueue(
        world,
        "ap.payment_recommendation",
        {"payee": "Acme", "invoice_ref": "A-1", "amount": "6000.00"},
    )
    at_terminal(True)
    assert _decide(world, "reject", card["id"], "--as", "sam") == 0


def test_nobody_approves_their_own_where_the_tenant_says_never(world, at_terminal, capsys):
    card = _enqueue(
        world,
        "expenses.reimbursement_record",
        {"report_id": "7", "person": "Jordan Hale", "month": "2026-09", "total_cents": "12000"},
        agent="expenses",
    )
    at_terminal(True)
    assert _decide(world, "approve", card["id"], "--as", "jordan-hale") == 2
    assert "own" in capsys.readouterr().err
    assert _decide(world, "approve", card["id"], "--as", "sam") == 0


def test_a_card_no_agent_described_is_never_one_agents_call(world, at_terminal, capsys):
    """Undescribed means the engine cannot say what money it commits, so
    only a full approver reaches it, and past the second-approver line it
    takes two. The completeness test keeps every real card described."""
    _policy(world, POLICY.replace('"approve:ap.invoice<=1000"', '"approve:ap.invoice"'))
    card = _enqueue(world, "ap.something_new", {"x": "1"})
    assert _decide(world, "approve", card["id"], "--as", "intake") == 2
    assert "no grant" in capsys.readouterr().err
    at_terminal(True)
    assert _decide(world, "approve", card["id"], "--as", "sam") == 0
    assert _card(world, "ap.something_new")["status"] == "pending"
    assert _decide(world, "approve", card["id"], "--as", "dana") == 0
    assert _card(world, "ap.something_new")["status"] == "approved"


# ---- what each card is -----------------------------------------------------------------

AGENTS = sorted(p.name for p in (REPO / "core/agents").iterdir() if (p / "jobs.py").is_file())


def _literal_card_types() -> set[tuple[str, str]]:
    """Every (agent, card type) the agents can raise: action_type literals,
    and the constants they assign card types to. The queue looks a card up
    by the agent that raised it, so that agent must describe it."""
    found: set[tuple[str, str]] = set()
    for path in (REPO / "core/agents").rglob("*.py"):
        if "evals" in path.parts:
            continue
        agent = path.relative_to(REPO / "core/agents").parts[0]
        text = path.read_text(encoding="utf-8")
        types = set(re.findall(r'action_type="([a-z_.]+)"', text))
        types |= set(re.findall(r'^[A-Z0-9_]+(?:CARD|ACTION) = "([a-z_]+\.[a-z_.]+)"', text, re.M))
        found |= {(agent, t) for t in types}
    return found


def test_every_card_type_says_what_it_is():
    missing = {(a, t) for a, t in _literal_card_types() if t not in load_card_authority(a)}
    assert not missing, f"card types with no CARD_AUTHORITY entry: {sorted(missing)}"


def test_every_declaration_names_a_real_action_and_resource():
    policy = parse_policy(
        {"money": {"out": "human"}, "roles": {"r": {"grants": []}}, "people": {}, "agents": {}}
    )
    assert policy.money_out == "human"
    from core.authority import ACTIONS, RESOURCES

    for agent in AGENTS:
        for card_type, rule in load_card_authority(agent).items():
            assert isinstance(rule, CardRule), card_type
            assert rule.action in ACTIONS, card_type
            assert rule.resource in RESOURCES, card_type


def test_a_lane_belongs_to_one_agent():
    with pytest.raises(AuthorityError, match="brief"):
        parse_policy(
            {
                "money": {"out": "human"},
                "roles": {"r": {"grants": []}},
                "agents": {
                    "a": {"roles": ["r"], "reads_outside": False, "lanes": ["brief"]},
                    "b": {"roles": ["r"], "reads_outside": False, "lanes": ["brief"]},
                },
            }
        )


# ---- the lanes: unattended becomes a grant ---------------------------------------------


def _brief_world(world, monkeypatch):
    with Ledger.open(resolve_ledger_root("demo", world["data"])) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="A-100",
            amount_cents=123456,
            due_date="2026-10-30",
        )
    ob = world["tmp"] / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    mailer = FakeMailer()
    monkeypatch.setattr(brief_jobs, "_send_client", lambda ctx: mailer)
    return ob, mailer


def _brief(world, ob, **params):
    base = {
        "today": "2026-10-05",
        "obligations_file": str(ob),
        "dir": str(world["tmp"] / "briefs"),
        "recipients": "owner@example.com",
    }
    return run("demo", "brief", "weekly", params={**base, **params}, ledger_dir=world["data"])


def test_a_lane_whose_agent_may_send_sends_and_leaves_a_decided_card(world, monkeypatch):
    ob, mailer = _brief_world(world, monkeypatch)
    _brief(world, ob)
    assert len(mailer.sent) == 1
    (card,) = [c for c in _cards(world) if c["action_type"] == brief_jobs.SEND_ACTION]
    assert card["status"] == "approved"
    assert card["params"]["decided_by"] == "courier"
    assert card["params"]["decided_via"] == "authority"


def test_under_authority_the_unattended_list_grants_nothing(world, monkeypatch):
    _policy(world, POLICY.replace('"send:message", ', ""))
    ob, mailer = _brief_world(world, monkeypatch)
    out = _brief(world, ob, unattended="send")
    assert mailer.sent == []
    assert out.status == "needs_approval"
    (card,) = [c for c in _cards(world) if c["action_type"] == brief_jobs.SEND_ACTION]
    assert card["status"] == "pending"


def test_a_grant_never_decides_a_card_a_person_was_already_asked(world, monkeypatch):
    _policy(world, POLICY.replace('"send:message", ', ""))
    ob, mailer = _brief_world(world, monkeypatch)
    _brief(world, ob)
    assert mailer.sent == []
    _policy(world, POLICY)
    _brief(world, ob, today="2026-10-06")
    # The pending card stays a question for a person; the grant does not
    # decide a card someone was already asked.
    assert mailer.sent == []


def test_the_calendar_lane_writes_when_its_agent_holds_send_calendar(world, monkeypatch):
    ob = world["tmp"] / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    cal = FakeCalendar()
    monkeypatch.setattr(cs, "_calendar_client", lambda ctx: cal)
    params = {"obligations_file": str(ob), "today": "2026-10-04", "calendar": "graph"}
    run("demo", "deadlines", "calendar", params=params, ledger_dir=world["data"])
    assert cal.calls
    (card,) = [c for c in _cards(world) if c["action_type"] == cs.CAL_ACTION]
    assert card["status"] == "approved"
    assert card["params"]["decided_by"] == "courier"


def test_the_calendar_lane_without_the_grant_parks_a_card(world, monkeypatch):
    _policy(world, POLICY.replace(', "send:calendar"', ""))
    ob = world["tmp"] / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    cal = FakeCalendar()
    monkeypatch.setattr(cs, "_calendar_client", lambda ctx: cal)
    params = {"obligations_file": str(ob), "today": "2026-10-04", "calendar": "graph"}
    out = run(
        "demo", "deadlines", "calendar", params={**params, "unattended": "calendar"},
        ledger_dir=world["data"],
    )  # fmt: skip
    assert cal.calls == []
    assert out.status == "needs_approval"


def test_doctor_says_auto_file_under_does_nothing_under_authority(world):
    from core.engine.doctor import run_doctor

    report = run_doctor("demo", tenants_root=world["root"])
    (line,) = [c for c in report.checks if c.name == "kit authority"]
    assert "auto_file_under = 100 does nothing" in line.detail


def test_doctor_says_when_no_one_can_decide_yet(world):
    from core.engine.doctor import run_doctor

    _policy(world, POLICY.split("[people.dana]")[0])
    report = run_doctor("demo", tenants_root=world["root"])
    (line,) = [c for c in report.checks if c.name == "kit authority"]
    assert "every card waits" in line.detail
