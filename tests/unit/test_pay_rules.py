"""A missionary's pay as a board's yearly resolution (c34, the sender model,
the owner's go of 2026-11-14).

A sending church pays its missionary a set amount from a set month, on a
set day, by a resolution of its board. The resolution is a plan (one card,
one yes) covering the one person it pays, so the floor already keeps that
person from adopting it. Pay adds what a plan says about money paid out:
the yearly amount, the day of the month, and the month it starts. It runs
through that December, so the board sets it again each year, and a
step-down is a new resolution from a later month.

Family abstains: a director who is family of the person a pay card covers
never decides it (``family`` in authority.toml, read both ways).
"""

from __future__ import annotations

import tomllib
from decimal import Decimal

import pytest

from core.authority import AuthorityError, Request, parse_policy
from core.authority import _floor as floor
from core.policy import Pay, PolicyError, Rule, Source, parse_layer, pay_for
from core.policy.pay import monthly_cents, write

PAY = """
layer = "tenant"
document = "Grace Fellowship pay for Ruth Hollis from January 2027"
plan = true
adopted_by = "board"

[[rule]]
id = "pay"
who = ["ruth-hollis"]
pay_year = "24000.00"
pay_day = 1
pay_from = "2027-01"
says = "Ruth Hollis is paid $24,000 a year, $2,000 on the 1st of each month."
source = { section = "Resolution", quote = "pays Ruth Hollis $24,000 a year" }
"""

ROLES = {"board", "missionary", "treasurer", "pastor"}
PEOPLE = {"ruth-hollis", "don-pruitt", "linda-hollis", "carol-jennings"}


def _layer(text: str = PAY, **kw):
    return parse_layer(tomllib.loads(text), "pay.toml", roles=ROLES, people=PEOPLE, **kw)


# ---- the rule ---------------------------------------------------------------------


def test_a_pay_resolution_reads_as_a_plan_covering_one_person():
    layer = _layer()
    assert layer.plan and layer.adopted_by == "board"
    [rule] = layer.rules
    assert rule.who == ("ruth-hollis",)
    assert (rule.pay_year, rule.pay_day, rule.pay_from) == (Decimal("24000.00"), 1, "2027-01")
    assert not rule.spending


def test_pay_belongs_to_a_plan():
    with pytest.raises(PolicyError, match="belong to a plan"):
        _layer(PAY.replace("plan = true\n", "").replace('adopted_by = "board"\n', ""))


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        ("pay_day = 1\n", "", "a yearly amount, a day of the month, and the month it starts"),
        ("pay_day = 1", "pay_day = 29", "1 to 28"),
        ('pay_from = "2027-01"', 'pay_from = "2027-13"', "a month, like 2027-01"),
        ('pay_from = "2027-01"', 'pay_from = "January"', "a month, like 2027-01"),
        ('pay_year = "24000.00"', 'pay_year = "0"', r"more than \$0"),
    ],
)
def test_a_pay_rule_says_all_three_and_says_them_right(old, new, said):
    with pytest.raises(PolicyError, match=said):
        _layer(PAY.replace(old, new))


@pytest.mark.parametrize(
    "who", ['["missionary"]', '["ruth-hollis", "don-pruitt"]', '["*"]', '["nobody-here"]']
)
def test_pay_names_one_person_the_ministry_knows(who):
    with pytest.raises(PolicyError):
        _layer(PAY.replace('who = ["ruth-hollis"]', f"who = {who}"))


def test_a_pay_rule_caps_nothing_and_needs_nothing_else():
    text = PAY.replace("pay_day = 1\n", 'pay_day = 1\nitem_up_to = "50"\n')
    with pytest.raises(PolicyError, match="pay says nothing else"):
        _layer(text)


def test_a_rule_without_pay_keeps_its_fingerprint():
    """Every rule a person said yes to before pay existed still matches it."""
    rule = Rule(
        id="meals",
        layer="tenant",
        says="A meal is up to $40.",
        source=Source(document="Policy", quote="Meals up to $40"),
        item_up_to=Decimal("40"),
    )
    assert rule.sha == _old_sha(rule)


def _old_sha(rule: Rule) -> str:
    """The fingerprint as it was computed before pay existed."""
    import hashlib
    import json
    from dataclasses import asdict

    plan = ("cash_up_to", "account_within", "return_within")
    gone = ("pay_year", "pay_day", "pay_from")
    fields = {
        k: v
        for k, v in asdict(rule).items()
        if k not in gone and (k not in plan or v is not None) and (k != "replaces" or v)
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


# ---- what is paid in a month --------------------------------------------------------


def test_pay_for_a_month_is_the_resolution_in_force_through_that_december():
    rules = list(_layer().rules)
    assert pay_for(rules, "ruth-hollis", "2026-12") is None
    pay = pay_for(rules, "ruth-hollis", "2027-03")
    assert pay == Pay(
        person="ruth-hollis",
        yearly_cents=2400000,
        cents=200000,
        day=1,
        start="2027-01",
        rule=rules[0],
    )
    assert pay_for(rules, "ruth-hollis", "2027-12").cents == 200000
    assert pay_for(rules, "ruth-hollis", "2028-01") is None  # the board sets it again
    assert pay_for(rules, "don-pruitt", "2027-03") is None


def test_a_step_down_from_a_later_month_takes_over_from_that_month():
    first = list(_layer().rules)
    later = list(
        _layer(
            PAY.replace("24000.00", "18000.00")
            .replace('pay_from = "2027-01"', 'pay_from = "2027-07"')
            .replace('id = "pay"', 'id = "pay-july"')
        ).rules
    )
    rules = first + later
    assert pay_for(rules, "ruth-hollis", "2027-06").cents == 200000
    assert pay_for(rules, "ruth-hollis", "2027-07").cents == 150000
    assert pay_for(rules, "ruth-hollis", "2027-12").start == "2027-07"


def test_a_year_that_does_not_split_evenly_pays_the_rest_in_december():
    assert [monthly_cents(100000, m) for m in (1, 11, 12)] == [8333, 8333, 8337]
    assert sum(monthly_cents(100000, m) for m in range(1, 13)) == 100000


# ---- the resolution's words ---------------------------------------------------------


def test_tim_drafts_the_resolution_with_the_abstain_line():
    written = write(
        "Grace Fellowship",
        person="ruth-hollis",
        name="Ruth Hollis",
        yearly=Decimal("24000"),
        day=1,
        start="2027-01",
        adopted_by="board",
    )
    words = written.words
    assert "Grace Fellowship" in words and "Ruth Hollis" in words
    assert "$24,000 a year" in words and "$2,000" in words and "1st" in words
    assert "January 2027 through December 2027" in words
    assert "Form 1099-NEC" in words
    assert "abstains" in words and "no family tie" in words
    assert "—" not in words  # no em dashes in anything a board signs
    layer = parse_layer(tomllib.loads(written.rules), written.file, roles=ROLES, people=PEOPLE)
    [rule] = layer.rules
    assert rule.pay_year == Decimal("24000.00") and rule.who == ("ruth-hollis",)
    assert layer.copy is not None and layer.copy.file == written.copy
    assert written.file == "pay-ruth-hollis-2027-01.toml"


def test_the_resolution_says_the_months_a_step_down_covers():
    written = write(
        "Grace Fellowship",
        person="ruth-hollis",
        name="Ruth Hollis",
        yearly=Decimal("18000"),
        day=15,
        start="2027-07",
        adopted_by="board",
    )
    assert "July 2027 through December 2027" in written.words
    assert "$1,500" in written.words and "15th" in written.words


# ---- family abstains ----------------------------------------------------------------

AUTHORITY = """
[money]
out = "human"

[roles.board]
grants = ["view:*", "confirm:authority"]

[roles.missionary]
grants = ["view:*@own", "submit:*@own"]

[people]
ruth-hollis = { roles = ["missionary"], scopes = ["ruth-hollis"] }
linda-hollis = { roles = ["board"], family = ["ruth-hollis"] }
don-pruitt = { roles = ["board"] }
"""


def _policy(text: str = AUTHORITY):
    return parse_policy(tomllib.loads(text))


def test_family_reads_both_ways():
    policy = _policy()
    assert policy.family_of("linda-hollis") == {"ruth-hollis"}
    assert policy.family_of("ruth-hollis") == {"linda-hollis"}
    assert policy.family_of("don-pruitt") == set()


@pytest.mark.parametrize(
    ("family", "said"),
    [('["nobody"]', "names another person"), ('["linda-hollis"]', "names another person")],
)
def test_family_names_other_people_in_the_file(family, said):
    text = AUTHORITY.replace('family = ["ruth-hollis"]', f"family = {family}")
    with pytest.raises(AuthorityError, match=said):
        _policy(text)


def _req(kin: bool) -> Request:
    return Request(
        "x", "confirm", "authority", via_card=True, covered=("ruth-hollis",), kin_abstain=kin
    )


def test_on_a_pay_card_family_of_the_person_it_covers_abstains():
    policy = _policy()
    linda, don, ruth = (policy.people[n] for n in ("linda-hollis", "don-pruitt", "ruth-hollis"))
    said = floor(policy, linda, _req(True))
    assert "family" in said and "Ruth Hollis" in said
    assert floor(policy, don, _req(True)) == ""
    assert "covers" in floor(policy, ruth, _req(True))


def test_family_decides_any_other_card_as_before():
    policy = _policy()
    assert floor(policy, policy.people["linda-hollis"], _req(False)) == ""
