"""Ask first (chunk 7, board items t-ask-first and t-month-remembers).

"Supplies over $1,000 need your reviewer's OK before you buy them." Tim
already says so before the spend; now the person asks for that OK in the
same breath. A card goes to whoever the rule names, and only they decide it.
Once they say yes, the month card remembers: the projector reads "OK'd by
Linda Park before it was bought", with the rule's page, not as over a rule.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from core.agents.policy.jobs import ASK_CARD, CARD_AUTHORITY
from core.authority import card_request, evaluate, parse_policy
from core.engine.runner import run
from core.policy import Needs, Ok, Rule, Source, Spent, check_month, with_oks
from core.tools.receipts import Claim
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _cards, _door, _month, _snap
from tests.unit.test_policy_before import RULES
from tests.unit.test_policy_month import _approve_rules, _rules

OK_FIRST = Rule(
    id="supplies-ok-first",
    layer="tenant",
    says="Supplies over $1,000 need your reviewer's OK before you buy them.",
    source=Source("Living Water spending policy", "Purchases are approved first.", "Buying", 4),
    kinds=("Ministry Supplies",),
    over=Decimal("1000"),
    needs=Needs(approver="reviewer", before=True),
)
PROJECTOR = Spent("p1", "Projector, Oct 20", "Ministry Supplies", 140000, True, day="2026-10-20")


# ---- core/policy: a yes given before the spend -------------------------------------


def test_a_line_with_no_ok_is_short_of_the_rule():
    [found] = check_month([OK_FIRST], [PROJECTOR], person="daniel-reyes", roles=())
    assert not found.met
    assert found.says.startswith("Projector, Oct 20, $1,400.00: Supplies over $1,000 need")


def test_an_ok_given_first_meets_the_rule_and_says_who_gave_it():
    ok = Ok("Linda Park", "Ministry Supplies", 150000, "2026-10-15", (OK_FIRST.sha,))
    [found] = check_month([OK_FIRST], with_oks([PROJECTOR], [ok]), person="daniel-reyes", roles=())
    assert found.met
    assert found.says == (
        "Projector, Oct 20, $1,400.00: OK'd by Linda Park before it was bought "
        "(Living Water spending policy, Buying, p. 4)"
    )


def test_an_ok_covers_one_line_of_its_kind_at_or_under_its_amount_from_its_day_on():
    ok = Ok("Linda Park", "Ministry Supplies", 150000, "2026-10-15", (OK_FIRST.sha,))
    before = Spent("p0", "Screen, Oct 9", "Ministry Supplies", 120000, True, day="2026-10-09")
    bigger = Spent("p2", "Sound desk, Oct 21", "Ministry Supplies", 180000, True, day="2026-10-21")
    second = Spent("p3", "Projector, Oct 22", "Ministry Supplies", 140000, True, day="2026-10-22")
    covered = with_oks([before, bigger, PROJECTOR, second], [ok])
    assert [s.key for s in covered if s.ok] == ["p1"]  # one OK, one purchase


def test_an_ok_counts_only_for_the_rules_it_was_asked_under():
    ok = Ok("Linda Park", "Ministry Supplies", 150000, "2026-10-15", ("another-rule",))
    [found] = check_month([OK_FIRST], with_oks([PROJECTOR], [ok]), person="daniel-reyes", roles=())
    assert not found.met


# ---- core/authority: the role a rule names decides the card ------------------------

POLICY = parse_policy(
    {
        "money": {"out": "human"},
        "roles": {
            "pastor": {"grants": ["approve:*"]},
            "treasurer": {"grants": ["approve:*"]},
            "missionary": {"grants": ["submit:*@own"]},
        },
        "people": {
            "mark-ellison": {"roles": ["pastor"]},
            "carol-jennings": {"roles": ["treasurer"]},
            "ruth-hollis": {"roles": ["missionary"], "scopes": ["ruth-hollis"]},
        },
    }
)
ASK = {"person": "Ruth Hollis", "amount": "300.00", "approver_role": "pastor"}


def test_only_the_role_a_rule_names_may_give_the_ok():
    rule = CARD_AUTHORITY[ASK_CARD]
    assert evaluate(POLICY, card_request(rule, "mark-ellison", ASK, POLICY)).allowed
    refused = evaluate(POLICY, card_request(rule, "carol-jennings", ASK, POLICY))
    assert not refused.allowed
    assert "pastor" in refused.reason


def test_a_card_that_names_no_role_goes_to_anyone_who_approves():
    rule = CARD_AUTHORITY[ASK_CARD]
    either = {**ASK, "approver_role": ""}
    assert evaluate(POLICY, card_request(rule, "carol-jennings", either, POLICY)).allowed


# ---- the ask job and the door ------------------------------------------------------


def _asks(solo) -> list[dict]:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    rows = con.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ?", (ASK_CARD,)
    ).fetchall()
    con.close()
    return [{"id": i, "status": s, **json.loads(p)} for i, s, p in rows]


def _ask(solo, what: str, amount: str):  # noqa: F811
    return run(
        "living-water",
        "policy",
        "ask",
        params={"person": "daniel-reyes", "what": what, "amount": amount, "today": "2026-10-15"},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def test_over_the_line_the_ask_raises_one_card_to_the_role_the_rule_names(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    out = _ask(solo, "a projector for the school", "1400")
    assert out.status == "needs_approval", out.summary
    [card] = _asks(solo)
    assert card["person"] == "Daniel Reyes"
    assert card["amount"] == "1400.00"
    assert card["kind"] == "Ministry Supplies"
    assert card["approver_role"] == "reviewer"
    assert card["asked_on"] == "2026-10-15"
    assert card["asked_of"] == "Linda Park"
    assert card["what"] == "an OK before buying a projector for the school"
    assert "p. 4" in card["rules"]
    _ask(solo, "a projector for the school", "1400")
    assert len(_asks(solo)) == 1  # asking again is the same card


def test_under_every_line_no_ok_is_needed_and_no_card_is_raised(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    out = _ask(solo, "printer paper", "60")
    assert out.status == "ok"
    assert _asks(solo) == []


def test_the_door_asks_and_says_whom(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    tim = _door(solo, "daniel-reyes")
    assert "ask_for_ok" in {s.name for s in tim.specs()}
    said = tim.call("ask_for_ok", {"what": "a projector", "amount": "1,400"})
    assert said["status"] == "asked"
    assert "Linda Park" in said["say"]
    assert "$1,400.00" in said["say"]
    again = tim.call("ask_for_ok", {"what": "a projector", "amount": "1,400"})
    assert again["status"] == "waiting"
    assert "Linda Park" in again["say"]


def test_a_ministry_with_no_rules_is_not_offered_the_ask(solo):  # noqa: F811
    assert "ask_for_ok" not in {s.name for s in _door(solo, "daniel-reyes").specs()}


# ---- the month card remembers -------------------------------------------------------


def test_the_month_card_names_the_ok_given_before_the_spend(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    tim = _door(solo, "daniel-reyes")
    tim.call("ask_for_ok", {"what": "a projector", "amount": "1,400", "kind": "Ministry Supplies"})
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    [(card, params)] = con.execute(
        "SELECT id, params_json FROM approval_queue WHERE action_type = ?", (ASK_CARD,)
    ).fetchall()
    params = json.loads(params) | {"asked_on": "2026-10-01", "decided_by": "linda-park"}
    con.execute(
        "UPDATE approval_queue SET status = 'approved', params_json = ? WHERE id = ?",
        (json.dumps(params), card),
    )
    con.commit()
    con.close()
    _snap(solo, "$1,400 for the projector, ministry supplies", Claim(Decimal("1400"), "USD", "$"))
    result = _month(solo)
    assert result.status != "error", result.summary
    [month] = _cards(solo)
    assert "OK'd by Linda Park before it was bought" in month["policy"]
    assert "need your reviewer's OK" not in month["policy"]


def test_an_ok_approved_after_the_month_card_went_up_renews_the_card(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    tim = _door(solo, "daniel-reyes")
    tim.call("ask_for_ok", {"what": "a projector", "amount": "1,400", "kind": "Ministry Supplies"})
    _snap(solo, "$1,400 for the projector, ministry supplies", Claim(Decimal("1400"), "USD", "$"))
    _month(solo)
    [first] = [c for c in _cards(solo) if c["status"] == "pending"]
    assert "need your reviewer's OK" in first["policy"]
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    [(card, params)] = con.execute(
        "SELECT id, params_json FROM approval_queue WHERE action_type = ?", (ASK_CARD,)
    ).fetchall()
    params = json.loads(params) | {"asked_on": "2026-10-01", "decided_by": "linda-park"}
    con.execute(
        "UPDATE approval_queue SET status = 'approved', params_json = ? WHERE id = ?",
        (json.dumps(params), card),
    )
    con.commit()
    con.close()
    _month(solo)
    [now] = [c for c in _cards(solo) if c["status"] == "pending"]
    assert "OK'd by Linda Park before it was bought" in now["policy"]
