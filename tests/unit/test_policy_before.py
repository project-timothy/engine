"""Before the spend: Tim answers from the ministry's rules (chunk 5, PR 4).

"Can I buy a $1,400 projector?" Tim reads the rules in force that cover the
person and the kind of spending, and says what applies at that amount, in
each rule's words with its page. It only advises: nothing is decided or
held, and the month card still checks the spending afterward with the same
code. A ministry with no rules is never offered the tool.
"""

from __future__ import annotations

from core.tools import catalog
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_policy_month import _approve_rules, _rules

RULES = """
layer = "tenant"
document = "Living Water spending policy"

[[rule]]
id = "meals-cap"
kinds = ["Meals"]
item_up_to = "40"
says = "A meal is up to $40."
source = { section = "Meals", page = 2, quote = "Meals are reimbursed up to $40 each." }

[[rule]]
id = "supplies-ok-first"
kinds = ["Ministry Supplies"]
over = "1000"
says = "Supplies over $1,000 need your reviewer's OK before you buy them."
needs = { approver = "reviewer", before = true }
source = { section = "Purchasing", page = 4, quote = "Purchases over $1,000 are approved first." }
"""


def _tim(solo, person: str = "daniel-reyes") -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        today="2026-10-13",
    )


def test_a_ministry_with_no_rules_is_not_offered_the_tool(solo):  # noqa: F811
    assert "ask_the_rules" not in {s.name for s in _tim(solo).specs()}


def test_rules_nobody_approved_are_not_in_force_yet(solo):  # noqa: F811
    _rules(solo, RULES)
    said = _tim(solo).call("ask_the_rules", {"what": "a projector", "amount": "1400"})
    assert said["rules"] == []
    assert "none of them is in force" in said["answer"]


def test_over_the_line_tim_names_the_rule_and_its_page(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    said = _tim(solo).call("ask_the_rules", {"what": "a new projector", "amount": "1,400"})
    assert said["kind"] == "Ministry Supplies"
    [rule] = [r for r in said["rules"] if r["applies"]]
    assert rule["says"] == "Supplies over $1,000 need your reviewer's OK before you buy them."
    assert rule["from"] == "Living Water spending policy, Purchasing, p. 4"
    assert "Supplies over $1,000 need your reviewer's OK" in said["answer"]
    assert "p. 4" in said["answer"]


def test_under_every_line_tim_says_nothing_stands_in_the_way(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    said = _tim(solo).call("ask_the_rules", {"what": "lunch with the team", "amount": "30"})
    assert said["kind"] == "Meals"
    assert not [r for r in said["rules"] if r["applies"]]
    assert said["answer"].startswith("Nothing in your ministry's rules stands in the way")
    assert "A meal is up to $40." in said["answer"]  # the rule that covers it, to keep in mind


def test_a_kind_the_words_do_not_settle_asks_which(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    said = _tim(solo).call("ask_the_rules", {"what": "a gift", "amount": "50"})
    assert said["kind"] == ""
    assert "Which of these is it" in said["answer"]
    assert "Meals" in said["answer"]


def test_without_an_amount_tim_reads_out_the_rules_for_that_kind(solo):  # noqa: F811
    _rules(solo, RULES)
    _approve_rules(solo)
    said = _tim(solo).call("ask_the_rules", {"what": "dinner"})
    assert [r["says"] for r in said["rules"]] == ["A meal is up to $40."]
    assert all(r["applies"] is None for r in said["rules"])
