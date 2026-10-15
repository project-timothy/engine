"""Policy rules (board item t-policy-rules, the owner's decisions of 2026-10-14).

A ministry's manuals become rules the engine can hold: who a rule covers,
what kind of spending, up to how much (an item, a month, a year), what it
needs (an approver, approval before buying, Face ID, a receipt, a receipt
that is not a bank statement, a budget line), and the words it came from.

Rules come in layers, handbook > executive > region > tenant > person, and
every layer's rules apply at once, so the strictest wins. A lower layer may
only make a rule stricter: one that overlaps a rule above it and asks for a
higher cap, or for an approver only at a higher amount, is refused with both
rules and the source named.

The handbook here is made up; a real ministry's handbook never enters this
repository.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from core.engine.authority_gate import load_rules
from core.engine.cli import main as engine_main
from core.engine.doctor import run_doctor
from core.engine.init import init_tenant
from core.policy import (
    LAYERS,
    PolicyError,
    Rulebook,
    Spent,
    check_month,
    load_rulebook,
    parse_layer,
)

HANDBOOK = """
layer = "handbook"
document = "Example Mission Field Handbook"

[[rule]]
id = "equipment-own-call"
kinds = ["Work Equipment"]
over = "1000"
says = "Work equipment over $1,000 needs your team leader's OK before you buy it."
needs = { approver = "team-leader", before = true }

[rule.source]
section = "Equipment for Work"
page = 21
quote = "Equipment over $1,000 must be approved by your team leader prior to purchase."

[[rule]]
id = "newsletter-cap"
kinds = ["Communication"]
month_up_to = "150"
says = "Newsletters and mailings: up to $150 a month."

[rule.source]
section = "Itinerating"
page = 7
quote = "An allowance of up to $150 per month covers newsletters."

[[rule]]
id = "no-bank-receipts"
says = "A bank statement or a copy of a check is not a receipt."
needs = { receipt_not_bank = true }

[rule.source]
section = "Itineration Reports and Receipts"
page = 6
quote = "Bank statements and copies of checks are not valid receipts."
"""


def _layer(text: str, name: str = "handbook.toml", **known):
    import tomllib

    return parse_layer(tomllib.loads(text), name, **known)


def _book(tmp_path: Path, files: dict[str, str], **known) -> Rulebook | None:
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    for name, text in files.items():
        (rules / name).write_text(text, encoding="utf-8")
    return load_rulebook(tmp_path, **known)


def test_a_handbook_layer_parses_into_rules_with_their_source():
    layer = _layer(HANDBOOK)
    assert layer.layer == "handbook"
    assert [r.id for r in layer.rules] == [
        "equipment-own-call",
        "newsletter-cap",
        "no-bank-receipts",
    ]
    equipment, newsletter, bank = layer.rules
    assert equipment.over == Decimal("1000")
    assert equipment.needs.approver == "team-leader"
    assert equipment.needs.before is True
    assert equipment.source.document == "Example Mission Field Handbook"
    assert equipment.source.page == 21
    assert newsletter.month_up_to == Decimal("150")
    assert newsletter.item_up_to is None
    assert bank.kinds == ("*",)
    assert bank.who == ("*",)
    assert bank.needs.receipt_not_bank is True


def test_a_rule_cites_its_source_in_plain_words():
    equipment = _layer(HANDBOOK).rules[0]
    assert equipment.cite() == "Example Mission Field Handbook, Equipment for Work, p. 21"


def test_a_rule_fingerprint_moves_when_any_word_moves():
    before = _layer(HANDBOOK).rules[1].sha
    after = _layer(HANDBOOK.replace("up to $150 per month", "up to $150 a month")).rules[1].sha
    assert before != after
    assert before == _layer(HANDBOOK).rules[1].sha


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ('layer = "handbook"', 'layer = "synod"', "layer"),
        ('quote = "Equipment over', 'note = "Equipment over', "quote"),
        ('month_up_to = "150"', 'month_up_to = "-150"', "month_up_to"),
        ('says = "Newsletters', 'said = "Newsletters', "says"),
    ],
)
def test_a_handbook_rule_must_say_what_it_needs(old, new, complaint):
    with pytest.raises(PolicyError, match=complaint):
        _layer(HANDBOOK.replace(old, new, 1))


def test_a_misspelled_field_is_refused_rather_than_ignored():
    with pytest.raises(PolicyError, match="item_upto"):
        _layer(HANDBOOK.replace('month_up_to = "150"', 'item_upto = "150"'))


def test_two_rules_with_one_id_are_refused():
    with pytest.raises(PolicyError, match="newsletter-cap"):
        _layer(HANDBOOK.replace('id = "no-bank-receipts"', 'id = "newsletter-cap"'))


def test_who_and_approvers_must_be_people_or_roles_the_ministry_named():
    with pytest.raises(PolicyError, match="team-leader"):
        _layer(HANDBOOK, roles={"missionary"}, people={"ruth-adeyemi"})
    layer = _layer(HANDBOOK, roles={"missionary", "team-leader"}, people={"ruth-adeyemi"})
    assert layer.rules[0].needs.approver == "team-leader"


def test_kinds_must_be_the_ministrys_own_categories_in_its_own_spelling():
    with pytest.raises(PolicyError, match="Work Equipment"):
        _layer(HANDBOOK, kinds=["Travel", "Communication"])
    layer = _layer(HANDBOOK, kinds=["work equipment", "communication"])
    assert layer.rules[0].kinds == ("work equipment",)


def test_no_rules_folder_means_no_rulebook(tmp_path):
    assert load_rulebook(tmp_path) is None


def test_layers_load_in_their_order_whatever_the_file_names(tmp_path):
    region = 'layer = "region"\ndocument = "Asia Pacific notes"\n'
    book = _book(tmp_path, {"a-region.toml": region, "z-handbook.toml": HANDBOOK})
    assert [layer.layer for layer in book.layers] == ["handbook", "region"]
    assert LAYERS.index("handbook") < LAYERS.index("region")


REGION_NEWSLETTER = """
layer = "region"
document = "Asia Pacific region policy"

[[rule]]
id = "newsletter-region"
kinds = ["Communication"]
month_up_to = "{cap}"
says = "Newsletters and mailings in Asia Pacific: up to ${cap} a month."
source = {{ quote = "Communication is capped at ${cap} a month in this region." }}
"""


def test_a_region_may_not_raise_a_handbook_cap(tmp_path):
    with pytest.raises(PolicyError) as refused:
        _book(tmp_path, {"h.toml": HANDBOOK, "r.toml": REGION_NEWSLETTER.format(cap=200)})
    said = str(refused.value)
    assert "newsletter-region" in said
    assert "newsletter-cap" in said
    assert "p. 7" in said
    assert "only stricter" in said


def test_a_region_may_lower_a_handbook_cap_and_both_apply(tmp_path):
    book = _book(tmp_path, {"h.toml": HANDBOOK, "r.toml": REGION_NEWSLETTER.format(cap=100)})
    ids = [r.id for r in book.applicable(person="ruth-adeyemi", roles=(), kind="Communication")]
    assert ids == ["newsletter-cap", "no-bank-receipts", "newsletter-region"]


TENANT_EQUIPMENT = """
layer = "tenant"
document = "Our church's spending policy"

[[rule]]
id = "equipment-tenant"
kinds = ["Work Equipment"]
over = "{over}"
says = "Equipment over ${over} needs your team leader's OK first."
needs = {{ approver = "team-leader", before = true }}
source = {{ quote = "Equipment over ${over} is approved in advance." }}
"""


def test_a_tenant_may_not_ask_for_the_approver_only_at_a_higher_amount(tmp_path):
    with pytest.raises(PolicyError, match="equipment-own-call"):
        _book(tmp_path, {"h.toml": HANDBOOK, "t.toml": TENANT_EQUIPMENT.format(over=2000)})


def test_a_tenant_may_ask_for_the_approver_sooner(tmp_path):
    book = _book(tmp_path, {"h.toml": HANDBOOK, "t.toml": TENANT_EQUIPMENT.format(over=500)})
    rules = book.applicable(person="ruth-adeyemi", roles=(), kind="Work Equipment")
    assert {r.id for r in rules} >= {"equipment-own-call", "equipment-tenant"}


def test_rules_that_do_not_overlap_never_conflict(tmp_path):
    other = TENANT_EQUIPMENT.format(over=2000).replace("Work Equipment", "Vehicles")
    book = _book(
        tmp_path,
        {"h.toml": HANDBOOK, "t.toml": other},
        kinds=["Work Equipment", "Communication", "Vehicles"],
    )
    assert [r.id for r in book.applicable(person="x", roles=(), kind="Vehicles")] == [
        "no-bank-receipts",
        "equipment-tenant",
    ]


PERSON = """
layer = "person"
person = "ruth-adeyemi"
document = "Ruth's own limits"

[[rule]]
id = "ruth-meals"
kinds = ["Meals"]
month_up_to = "60"
says = "Ruth keeps meals to $60 a month."
source = { quote = "I want to keep meals under $60 a month." }
"""


def test_a_person_layer_covers_only_that_person(tmp_path):
    book = _book(tmp_path, {"h.toml": HANDBOOK, "p.toml": PERSON})
    assert "ruth-meals" in [
        r.id for r in book.applicable(person="ruth-adeyemi", roles=(), kind="Meals")
    ]
    assert "ruth-meals" not in [
        r.id for r in book.applicable(person="tom-baker", roles=(), kind="Meals")
    ]


def test_a_person_layer_names_a_person_the_ministry_knows():
    with pytest.raises(PolicyError, match="person"):
        _layer(PERSON.replace('person = "ruth-adeyemi"\n', ""), "p.toml")
    with pytest.raises(PolicyError, match="ruth-adeyemi"):
        _layer(PERSON, "p.toml", roles=set(), people={"tom-baker"})


def test_who_narrows_a_rule_to_roles_or_people():
    text = HANDBOOK.replace('id = "newsletter-cap"', 'id = "newsletter-cap"\nwho = ["missionary"]')
    book = Rulebook((_layer(text),))
    assert "newsletter-cap" in [
        r.id for r in book.applicable(person="ruth", roles=("missionary",), kind="Communication")
    ]
    assert "newsletter-cap" not in [
        r.id for r in book.applicable(person="carol", roles=("treasurer",), kind="Communication")
    ]


# ---- a ministry's rules, loaded with its own names --------------------------------


CHURCH = """
layer = "tenant"
document = "Grace Fellowship spending policy"

[[rule]]
id = "meals-cap"
kinds = ["Meals"]
item_up_to = "40"
says = "A meal is up to $40."
source = { section = "Meals", quote = "Meals are reimbursed up to $40 each." }

[[rule]]
id = "pastor-over-250"
who = ["missionary"]
over = "250"
says = "Anything over $250 needs the pastor's OK."
needs = { approver = "pastor" }
source = { quote = "Spending over $250 is approved by the pastor." }
"""


@pytest.fixture
def church(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    result = init_tenant("grace", root=root, run_audit=False, shape="nonprofit-small")
    return root, result.tenant_dir


def test_a_ministry_with_no_rules_has_no_rulebook(church):
    root, _ = church
    assert load_rules("grace", tenants_root=root) is None


def test_a_ministrys_rules_load_against_its_own_roles_and_categories(church):
    root, directory = church
    (directory / "rules").mkdir()
    (directory / "rules" / "church.toml").write_text(CHURCH, encoding="utf-8")
    book = load_rules("grace", tenants_root=root)
    assert [r.id for r in book.rules] == ["meals-cap", "pastor-over-250"]
    (directory / "rules" / "church.toml").write_text(
        CHURCH.replace('approver = "pastor"', 'approver = "bishop"'), encoding="utf-8"
    )
    with pytest.raises(PolicyError, match="bishop"):
        load_rules("grace", tenants_root=root)


def test_doctor_names_a_broken_rules_file(church):
    root, directory = church
    lines = {c.name: c for c in run_doctor("grace", tenants_root=root, env={}).checks}
    assert lines["policy rules"].status == "skip"
    (directory / "rules").mkdir()
    (directory / "rules" / "church.toml").write_text(CHURCH, encoding="utf-8")
    lines = {c.name: c for c in run_doctor("grace", tenants_root=root, env={}).checks}
    assert lines["policy rules"].status == "ok"
    assert "2 rules" in lines["policy rules"].detail
    (directory / "rules" / "church.toml").write_text(
        CHURCH.replace('kinds = ["Meals"]', 'kinds = ["Feasts"]'), encoding="utf-8"
    )
    lines = {c.name: c for c in run_doctor("grace", tenants_root=root, env={}).checks}
    assert lines["policy rules"].status == "missing"
    assert "Feasts" in lines["policy rules"].detail


def test_engine_rules_prints_each_rule_with_its_source(church, capsys):
    root, directory = church
    (directory / "rules").mkdir()
    (directory / "rules" / "church.toml").write_text(CHURCH, encoding="utf-8")
    assert engine_main(["rules", "grace"]) == 0
    out = capsys.readouterr().out
    assert "Grace Fellowship spending policy (tenant)" in out
    assert "A meal is up to $40." in out
    assert "Grace Fellowship spending policy, Meals" in out
    assert '"Meals are reimbursed up to $40 each."' in out


# ---- after the spend: a month against the rules in force --------------------------


def _rules(text: str = HANDBOOK):
    return list(_layer(text).rules)


def test_a_line_over_an_item_cap_is_named_with_the_rule_and_its_page():
    caps = """
layer = "tenant"
document = "Grace Fellowship spending policy"

[[rule]]
id = "meals-cap"
kinds = ["Meals"]
item_up_to = "40"
says = "A meal is up to $40."
source = { section = "Meals", page = 2, quote = "Meals up to $40 each." }
"""
    found = check_month(
        _rules(caps),
        [
            Spent("a", "Dinner, Oct 3", "Meals", 5200, receipt=True),
            Spent("b", "Lunch, Oct 4", "Meals", 1800, receipt=True),
        ],
        person="ruth",
        roles=(),
    )
    assert [f.says for f in found] == [
        "Dinner, Oct 3, $52.00: A meal is up to $40. "
        "(Grace Fellowship spending policy, Meals, p. 2)"
    ]
    assert found[0].keys == ("a",)


def test_a_month_over_its_cap_counts_every_line_of_that_kind():
    found = check_month(
        _rules(),
        [
            Spent("a", "Prayer cards", "Communication", 9000, receipt=True),
            Spent("b", "Postage", "Communication", 7000, receipt=True),
            Spent("c", "Bus", "Travel", 50000, receipt=True),
        ],
        person="ruth",
        roles=(),
    )
    [over] = [f for f in found if f.rule.id == "newsletter-cap"]
    assert over.says.startswith("Communication this month, $160.00: Newsletters and mailings")
    assert over.keys == ("a", "b")


def test_a_year_cap_counts_what_was_already_reported_this_year():
    year = HANDBOOK.replace('month_up_to = "150"', 'year_up_to = "1500"')
    spent = [Spent("a", "Website", "Communication", 20000, receipt=True)]
    assert not [
        f
        for f in check_month(_rules(year), spent, person="r", roles=())
        if f.rule.id == "newsletter-cap"
    ]
    [over] = [
        f
        for f in check_month(
            _rules(year), spent, person="r", roles=(), year_before={"Communication": 140000}
        )
        if f.rule.id == "newsletter-cap"
    ]
    assert "this year, $1,600.00" in over.says


def test_needs_apply_from_their_amount_on():
    found = check_month(
        _rules(),
        [
            Spent("a", "Laptop", "Work Equipment", 140000, receipt=True),
            Spent("b", "Mouse", "Work Equipment", 2500, receipt=True),
        ],
        person="ruth",
        roles=(),
    )
    [needs] = [f for f in found if f.rule.id == "equipment-own-call"]
    assert needs.keys == ("a",)
    assert needs.says.startswith("Laptop, $1,400.00: Work equipment over $1,000 needs")


def test_a_bank_line_with_no_receipt_behind_it_is_not_a_receipt():
    found = check_month(
        _rules(),
        [
            Spent("a", "Shoprite, Oct 3", "Meals", 3000, receipt=False, bank=True),
            Spent("b", "Shoprite, Oct 4", "Meals", 3000, receipt=True, bank=True),
            Spent("c", "Taxi, Oct 5", "Travel", 1000, receipt=True),
        ],
        person="ruth",
        roles=(),
    )
    [bank] = [f for f in found if f.rule.id == "no-bank-receipts"]
    assert bank.keys == ("a",)


def test_a_face_id_rule_says_so_on_the_finding():
    text = HANDBOOK.replace("needs = { receipt_not_bank = true }", "needs = { face_id = true }")
    text = text.replace('id = "no-bank-receipts"', 'id = "face-over-100"\nover = "100"')
    found = check_month(
        _rules(text), [Spent("a", "Hotel", "Lodging", 15000, receipt=True)], person="r", roles=()
    )
    [face] = [f for f in found if f.rule.id == "face-over-100"]
    assert face.face_id is True


def test_rules_for_someone_else_find_nothing():
    text = HANDBOOK.replace('id = "newsletter-cap"', 'id = "newsletter-cap"\nwho = ["tom-baker"]')
    spent = [Spent("a", "Postage", "Communication", 90000, receipt=True)]
    found = check_month(_rules(text), spent, person="ruth", roles=("missionary",))
    assert not [f for f in found if f.rule.id == "newsletter-cap"]
