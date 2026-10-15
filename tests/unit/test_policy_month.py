"""After the spend: the month card says what the rules in force say (chunk 5).

Daniel's $52 dinner meets his ministry's "a meal is up to $40". Once a
person has said yes to that rule, the reviewer's card names the dinner, the
rule in Tim's words, and the page it came from; a rule nobody approved says
nothing, and a ministry with no rules gets the card it always got.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from core.agents.policy.jobs import RULE_CARD
from core.engine.runner import run
from core.tools.receipts import Claim
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _cards, _month, _snap

RULES = """
layer = "tenant"
document = "Living Water spending policy"

[[rule]]
id = "meals-cap"
kinds = ["Meals"]
item_up_to = "40"
says = "A meal is up to $40."
source = { section = "Meals", page = 2, quote = "Meals are reimbursed up to $40 each." }
"""


def _rules(solo, text: str = RULES) -> None:  # noqa: F811
    folder = Path(solo["root"]) / "living-water" / "rules"
    folder.mkdir(exist_ok=True)
    (folder / "policy.toml").write_text(text, encoding="utf-8")


def _approve_rules(solo) -> None:  # noqa: F811
    out = run(
        "living-water",
        "policy",
        "propose",
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )
    assert out.status == "needs_approval", out.summary
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute("UPDATE approval_queue SET status = 'approved' WHERE action_type = ?", (RULE_CARD,))
    con.commit()
    con.close()


def _dinner(solo) -> None:  # noqa: F811
    _snap(solo, "$52 for dinner with the pastors", Claim(Decimal("52"), "USD", "$"))


def test_a_ministry_with_no_rules_gets_the_card_it_always_got(solo):  # noqa: F811
    _dinner(solo)
    _month(solo)
    [card] = _cards(solo)
    assert "policy" not in card and "policy_face_id" not in card


def test_a_rule_nobody_approved_says_nothing(solo):  # noqa: F811
    _rules(solo)
    _dinner(solo)
    _month(solo)
    [card] = _cards(solo)
    assert "policy" not in card


def test_an_approved_rule_names_the_line_and_its_page(solo):  # noqa: F811
    _rules(solo)
    _approve_rules(solo)
    _dinner(solo)
    result = _month(solo)
    assert result.status != "error", result.summary
    [card] = _cards(solo)
    assert card["policy"] == (
        "Meals, Oct 12, $52.00: A meal is up to $40. (Living Water spending policy, Meals, p. 2)"
    )


def test_a_rule_approved_after_the_card_brings_a_new_card(solo):  # noqa: F811
    _rules(solo)
    _dinner(solo)
    _month(solo)
    _approve_rules(solo)
    _month(solo)
    cards = _cards(solo)
    assert [c["status"] for c in cards] == ["superseded", "pending"]
    assert "A meal is up to $40." in cards[1]["policy"]


def test_a_face_id_rule_keeps_the_word_from_deciding(solo):  # noqa: F811
    _rules(
        solo,
        RULES.replace('item_up_to = "40"', 'over = "25"\nneeds = { face_id = true }').replace(
            "A meal is up to $40.", "A meal over $25 takes your Face ID."
        ),
    )
    _approve_rules(solo)
    _dinner(solo)
    _month(solo)
    [card] = _cards(solo)
    assert card["policy_face_id"] == "true"


def test_tim_and_the_month_page_say_what_the_card_says(solo):  # noqa: F811
    from core.tools import month_page
    from tests.unit.test_missionary_month import _reviewer

    _rules(solo)
    _approve_rules(solo)
    _dinner(solo)
    _month(solo)
    [person] = _reviewer(solo).call("month_report", {"month": "2026-10"})["people"]
    assert person["rules_say"] == [
        "Meals, Oct 12, $52.00: A meal is up to $40. (Living Water spending policy, Meals, p. 2)"
    ]
    page = month_page.render(
        person, title="Daniel's October", ministry="Living Water", approve=None, nonce="n"
    )
    assert "What the ministry's rules say" in page
    assert "A meal is up to $40." in page


def test_no_rules_no_rules_say(solo):  # noqa: F811
    from tests.unit.test_missionary_month import _reviewer

    _dinner(solo)
    _month(solo)
    [person] = _reviewer(solo).call("month_report", {"month": "2026-10"})["people"]
    assert "rules_say" not in person
