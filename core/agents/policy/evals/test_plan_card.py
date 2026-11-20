"""policy/propose: an accountable plan goes in force whole, on one yes, from
someone it does not cover (chunk 3, the owner's go of 2026-10-16).

A solo ministry's missionary holds every grant and may approve their own,
so nothing in authority.toml stops them adopting the plan that governs their
own cash. The floor does: the plan card names whom it covers, and none of
them decides it. Their board reviewer, who decides nothing else, adopts it.
Once adopted, the plan's numbers drive the cash clock.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from core.agents.policy.jobs import PLAN_CARD, RULE_CARD, clock_in_force, rules_in_force
from core.engine.authority_gate import decide_card, load_policy
from core.engine.init import init_tenant
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.policy import Clock, PolicyError, parse_layer

PLAN = """
layer = "tenant"
document = "Living Water accountable plan"
plan = true
adopted_by = "reviewer"

[[rule]]
id = "cash-on-hand"
who = ["missionary"]
cash_up_to = "1500"
says = "Cash on hand stays at or under $1,500."
source = { section = "5", quote = "Cash on hand is not to exceed the equivalent of $1,500." }

[[rule]]
id = "account-within"
who = ["missionary"]
account_within = 30
says = "Every expense is accounted for within 30 days."
source = { section = "4", quote = "Within 30 days after paying an expense" }

[[rule]]
id = "return-within"
who = ["missionary"]
return_within = 90
says = "What is not accounted for goes back within 90 days."
source = { section = "6", quote = "returned within 90 days" }

[[rule]]
id = "receipt-from-75"
who = ["missionary"]
over = "74.99"
needs = { receipt = true }
says = "A receipt is needed for anything $75 or more."
source = { section = "4", quote = "A receipt is required for any single expense of $75 or more." }
"""

PEOPLE = """
[people]
daniel-reyes = { roles = ["missionary"], scopes = ["daniel-reyes"] }
linda-park = { roles = ["reviewer"] }
"""


@pytest.fixture
def solo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    directory = init_tenant(
        "living-water", root=root, run_audit=False, shape="nonprofit-solo"
    ).tenant_dir
    authority = directory / "authority.toml"
    authority.write_text(
        authority.read_text(encoding="utf-8").replace("[people]\n", PEOPLE.lstrip()),
        encoding="utf-8",
    )
    (directory / "rules").mkdir()
    (directory / "rules" / "plan.toml").write_text(PLAN, encoding="utf-8")
    return {"root": root, "dir": directory}


def _propose(solo):
    return run("living-water", "policy", "propose", tenants_root=solo["root"])


def _cards(action):
    with Ledger.open(resolve_ledger_root("living-water")) as ledger:
        rows = ledger.conn.execute(
            "SELECT * FROM approval_queue WHERE action_type = ? ORDER BY id", (action,)
        ).fetchall()
    return [{**dict(r), "params": json.loads(r["params_json"])} for r in rows]


def _decide(solo, who, row):
    policy = load_policy("living-water", tenants_root=solo["root"]).policy
    return decide_card(
        policy,
        who,
        row,
        decision="approved",
        at_terminal=True,
        delegations=[],
        today=date(2026, 10, 16),
        now="2026-10-16T20:00:00+00:00",
    )


def _set(card_id, status):
    with Ledger.open(resolve_ledger_root("living-water")) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = ? WHERE id = ?", (status, card_id))
        ledger.conn.commit()


def _in_force(solo):
    authority = load_policy("living-water", tenants_root=solo["root"])
    with Ledger.open(resolve_ledger_root("living-water")) as ledger:
        return [r.id for r in rules_in_force(ledger, "living-water", authority.rules)]


def _clock(solo, person="Daniel Reyes"):
    authority = load_policy("living-water", tenants_root=solo["root"])
    with Ledger.open(resolve_ledger_root("living-water")) as ledger:
        return clock_in_force(
            ledger.conn, "living-water", authority.rules, authority.policy, person
        )


def test_the_plan_is_one_card_naming_every_rule(solo):
    assert _propose(solo).status == "needs_approval"
    assert _cards(RULE_CARD) == []
    [card] = _cards(PLAN_CARD)
    params = card["params"]
    assert params["what"] == "the Living Water accountable plan"
    assert params["adopted_by"] == "reviewer"
    assert params["covers"] == "missionary"
    assert len(params["shas"].split(",")) == 4
    assert (
        "Cash on hand stays at or under $1,500 (Living Water accountable plan, 5)"
        in (params["lines"])
    )
    assert _propose(solo).status in ("ok", "skipped")
    assert len(_cards(PLAN_CARD)) == 1


def test_one_yes_puts_the_whole_plan_in_force(solo):
    _propose(solo)
    assert _in_force(solo) == []
    _set(_cards(PLAN_CARD)[0]["id"], "approved")
    assert _in_force(solo) == [
        "cash-on-hand",
        "account-within",
        "return-within",
        "receipt-from-75",
    ]


@pytest.mark.security("SEC-05")
def test_the_person_it_covers_never_adopts_it_even_one_who_approves_their_own(solo):
    _propose(solo)
    row = _cards(PLAN_CARD)[0]
    refused = _decide(solo, "daniel-reyes", row).refusal
    assert "covers daniel-reyes" in refused
    assert _decide(solo, "bookkeeper", row).refusal  # no agent confirms authority
    assert not _decide(solo, "linda-park", row).refusal


def test_new_words_are_a_new_card_and_the_old_one_retires(solo):
    _propose(solo)
    [old] = _cards(PLAN_CARD)
    path = solo["dir"] / "rules" / "plan.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("= 30", "= 45"), encoding="utf-8")
    _propose(solo)
    cards = {c["id"]: c for c in _cards(PLAN_CARD)}
    assert len(cards) == 2
    assert cards[old["id"]]["status"] == "superseded"


def test_the_clock_runs_on_the_adopted_numbers_and_on_the_old_days_before(solo):
    _propose(solo)
    assert _clock(solo) == Clock(due=60, back=120, cash_up_to=None)
    assert _clock(solo).nudge == 45
    _set(_cards(PLAN_CARD)[0]["id"], "approved")
    adopted = _clock(solo)
    assert adopted == Clock(due=30, back=90, cash_up_to=Decimal("1500.00"))
    assert adopted.nudge == 15
    assert _clock(solo, "Linda Park") == Clock()  # the plan does not cover her


def _layer(text):
    import tomllib

    return parse_layer(tomllib.loads(text), "plan.toml", roles={"missionary", "reviewer"})


@pytest.mark.security("SEC-05")
def test_a_plan_that_covers_its_adopter_does_not_load():
    with pytest.raises(PolicyError, match="covers whoever adopts it"):
        _layer(PLAN.replace('adopted_by = "reviewer"', 'adopted_by = "missionary"'))
    with pytest.raises(PolicyError, match="covers whoever adopts it"):
        _layer(PLAN.replace('who = ["missionary"]\ncash', 'who = ["*"]\ncash'))


def test_a_plan_names_an_adopter_and_only_a_plan_holds_cash_and_days():
    with pytest.raises(PolicyError, match="says who adopts it"):
        _layer(PLAN.replace('adopted_by = "reviewer"\n', ""))
    with pytest.raises(PolicyError, match="belong to a plan"):
        _layer(PLAN.replace("plan = true\n", "").replace('adopted_by = "reviewer"\n', ""))
    with pytest.raises(PolicyError, match="number of days"):
        _layer(PLAN.replace("= 30", '= "thirty"'))


def test_the_person_it_covers_hears_why_in_plain_words(solo):
    from core.tools.decide import plain_refusal

    _propose(solo)
    row = _cards(PLAN_CARD)[0]
    said = plain_refusal(_decide(solo, "daniel-reyes", row).refusal, row, "approve")
    assert said.startswith("This covers you, so someone it doesn't cover decides it")
    assert "daniel" not in said.casefold()
