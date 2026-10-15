"""policy/propose: each rule goes live only on a person's yes (chunk 5, PR 2).

A model may draft a rule from a handbook; nothing it drafts enforces. The
propose job raises one card per rule, keyed to the rule's exact words and
amounts, and a rule is in force only while an approved card carries its
fingerprint. Deciding the card is confirming an authority change, so the
floor holds: no agent decides it, and a card that commits no money always
takes the person's own Face ID through the door. During the alpha the
owner approves every rule (2026-10-14).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from core.agents.policy.jobs import RULE_CARD, rules_in_force
from core.engine.authority_gate import decide_card, load_policy
from core.engine.init import init_tenant
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

RULES = """
layer = "tenant"
document = "Grace Fellowship spending policy"

[[rule]]
id = "meals-cap"
kinds = ["Meals"]
item_up_to = "40"
says = "A meal is up to $40."
source = { section = "Meals", page = 2, quote = "Meals are reimbursed up to $40 each." }

[[rule]]
id = "pastor-over-250"
over = "250"
says = "Anything over $250 needs the pastor's OK."
needs = { approver = "pastor" }
source = { quote = "Spending over $250 is approved by the pastor." }
"""

PEOPLE = """
[people]
carol-jennings = { roles = ["board"] }
ruth-adeyemi = { roles = ["missionary"], scopes = ["ruth-adeyemi"] }
"""


@pytest.fixture
def church(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    directory = init_tenant("grace", root=root, run_audit=False, shape="nonprofit-small").tenant_dir
    authority = directory / "authority.toml"
    authority.write_text(
        authority.read_text(encoding="utf-8").replace("[people]\n", PEOPLE.lstrip()),
        encoding="utf-8",
    )
    (directory / "rules").mkdir()
    (directory / "rules" / "church.toml").write_text(RULES, encoding="utf-8")
    return {"root": root, "dir": directory, "ledger": tmp_path / "ledger"}


def _propose(church):
    return run("grace", "policy", "propose", tenants_root=church["root"])


def _cards(church):
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        rows = ledger.conn.execute(
            "SELECT id, status, action_type, agent, params_json FROM approval_queue "
            "WHERE action_type = ? ORDER BY id",
            (RULE_CARD,),
        ).fetchall()
    return [{**dict(r), "params": json.loads(r["params_json"])} for r in rows]


def _set(card_id, status):
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = ? WHERE id = ?", (status, card_id))
        ledger.conn.commit()


def _in_force(church):
    authority = load_policy("grace", tenants_root=church["root"])
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        return [r.id for r in rules_in_force(ledger, "grace", authority.rules)]


def test_a_ministry_with_no_rules_is_left_alone(church):
    (church["dir"] / "rules" / "church.toml").unlink()
    out = _propose(church)
    assert out.status == "ok"
    assert "no rules" in out.summary
    assert _cards(church) == []


def test_each_rule_gets_one_card_in_plain_words(church):
    out = _propose(church)
    assert out.status == "needs_approval"
    cards = _cards(church)
    assert [c["params"]["rule"] for c in cards] == ["meals-cap", "pastor-over-250"]
    meals = cards[0]["params"]
    assert meals["says"] == "A meal is up to $40."
    assert meals["from"] == "Grace Fellowship spending policy, Meals, p. 2"
    assert meals["quote"] == "Meals are reimbursed up to $40 each."
    assert meals["layer"] == "tenant"
    assert meals["what"] == (
        'the rule "A meal is up to $40." from Grace Fellowship spending policy, Meals, p. 2'
    )
    assert len(meals["sha"]) == 64
    assert _propose(church).status in ("ok", "skipped")
    assert len(_cards(church)) == 2


def test_nothing_is_in_force_until_its_card_is_approved(church):
    _propose(church)
    assert _in_force(church) == []
    meals, pastor = _cards(church)
    _set(meals["id"], "approved")
    _set(pastor["id"], "rejected")
    assert _in_force(church) == ["meals-cap"]


def test_new_words_need_a_new_yes(church):
    _propose(church)
    meals, pastor = _cards(church)
    _set(meals["id"], "approved")
    path = church["dir"] / "rules" / "church.toml"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('item_up_to = "40"', 'item_up_to = "35"')
        .replace("$40", "$35")
        .replace("Spending over $250", "Any spending over $250"),
        encoding="utf-8",
    )
    _propose(church)
    cards = {c["id"]: c for c in _cards(church)}
    assert len(cards) == 4
    assert cards[pastor["id"]]["status"] == "superseded"
    assert _in_force(church) == []


def test_a_person_who_may_confirm_authority_decides_a_rule_and_no_one_else(church):
    _propose(church)
    meals = _cards(church)[0]
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        row = dict(
            ledger.conn.execute(
                "SELECT * FROM approval_queue WHERE id = ?", (meals["id"],)
            ).fetchone()
        )
    row["params"] = json.loads(row["params_json"])
    policy = load_policy("grace", tenants_root=church["root"]).policy

    def decide(who):
        return decide_card(
            policy,
            who,
            row,
            decision="approved",
            at_terminal=True,
            delegations=[],
            today=date(2026, 10, 14),
            now="2026-10-14T20:00:00+00:00",
        )

    assert decide("ruth-adeyemi").refusal
    assert decide("intake").refusal
    assert not decide("carol-jennings").refusal


def test_a_broken_rules_file_says_why_and_raises_nothing(church):
    path = church["dir"] / "rules" / "church.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"Meals"', '"Feasts"'), encoding="utf-8"
    )
    out = _propose(church)
    assert out.status == "error"
    assert "Feasts" in json.dumps([a.detail for a in out.anomalies])
    assert _cards(church) == []
