"""The authority model (issue #432, docs/tenant-kit-design.md section 3).

`core/authority` answers one question: may this principal take this action
on this resource, in this context? The request is shaped the way Cedar shapes
one (principal, action, resource, context); the answer is default deny, a
forbid always wins, scopes stop sideways movement, and nobody grants what
they do not hold.

Two layers. The floor lives in code and no `authority.toml` can switch it
off: money and external sends happen only through a card, agents never
release money or confirm an authority change, and an agent that reads outside
documents never proposes one. The safeguards live in data, set per shape and
changeable by the tenant: self-approval, a second approver, a distinct payer,
who confirms an authority change, and the monthly review-after.

Nothing in the daily loop reads this yet (#435 wires it into the queue).
"""

from __future__ import annotations

import itertools
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from core.authority import (
    ACTIONS,
    RESOURCES,
    AuthorityError,
    Principal,
    Request,
    Role,
    change_record,
    check_change,
    evaluate,
    may_grant,
    parse_permission,
    parse_policy,
)
from core.engine.init import render
from core.engine.kit import SHAPES, KitError, load_kit

REPO = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO / "tenants" / "demo"


def _policy(text: str):
    return parse_policy(tomllib.loads(text))


BASE = """
[money]
out = "human"

[safeguards]
self_approval = "never"
self_approval_limit = 0
review_after = "none"
distinct_payer = "off"
distinct_payer_above = 0
second_approver_above = 0
authority_confirm = "second_person"
report_self_approvals = "never"

[roles.owner]
grants = ["*:*"]

[roles.approver]
grants = ["view:*", "approve:expense.report@own<=500"]

[roles.nothing]
grants = []

[roles.no-invoices]
grants = ["approve:*"]
forbids = ["approve:ap.invoice"]

[roles.admin-agent]
grants = ["view:*", "propose:authority"]

[people.pat]
roles = ["owner"]

[people.sam]
roles = ["approver"]
scopes = ["unit-a"]

[people.nell]
roles = ["nothing"]

[people.fran]
roles = ["no-invoices"]

[agents.admin]
roles = ["admin-agent"]
reads_outside = false

[agents.mailroom]
roles = ["owner"]
reads_outside = true
"""


@pytest.fixture
def policy():
    return _policy(BASE)


def _with(**safeguards) -> object:
    text = BASE
    for key, value in safeguards.items():
        rendered = f'"{value}"' if isinstance(value, str) else str(value)
        lines = [ln for ln in text.splitlines() if not ln.startswith(f"{key} =")]
        index = lines.index("[safeguards]") + 1
        lines.insert(index, f"{key} = {rendered}")
        text = "\n".join(lines)
    return _policy(text)


# ---- the permission grammar ----------------------------------------------------


def test_a_permission_names_action_resource_scope_and_limit():
    perm = parse_permission("approve:expense.report@own<=500")
    assert (perm.action, perm.resource, perm.scope, perm.limit) == (
        "approve",
        "expense.report",
        "own",
        Decimal("500"),
    )


def test_scope_defaults_to_any_and_limit_to_none():
    perm = parse_permission("view:*")
    assert (perm.scope, perm.limit) == ("any", None)


@pytest.mark.parametrize(
    "text",
    ["fly:ap.invoice", "approve:spaceship", "approve:ap.invoice@nearby", "approve", "approve:*<=x"],
)
def test_a_malformed_permission_refuses(text):
    with pytest.raises(AuthorityError):
        parse_permission(text)


def test_a_role_a_person_names_must_exist():
    with pytest.raises(AuthorityError, match="ghost"):
        _policy(BASE + '\n[people.zed]\nroles = ["ghost"]\n')


def test_an_agent_must_say_whether_it_reads_outside_documents():
    with pytest.raises(AuthorityError, match="reads_outside"):
        _policy(BASE + '\n[agents.quiet]\nroles = ["nothing"]\n')


# ---- the core rules -------------------------------------------------------------


def test_default_deny(policy):
    decision = evaluate(policy, Request("nell", "view", "ap.invoice"))
    assert not decision.allowed and "no grant" in decision.reason


def test_an_unknown_principal_is_denied(policy):
    assert not evaluate(policy, Request("mallory", "view", "ap.invoice")).allowed


def test_a_forbid_wins_over_a_grant(policy):
    assert evaluate(policy, Request("fran", "approve", "expense.report", submitter="x")).allowed
    decision = evaluate(policy, Request("fran", "approve", "ap.invoice", submitter="x"))
    assert not decision.allowed and "forbid" in decision.reason


def test_scope_stops_sideways_movement(policy):
    own = Request("sam", "approve", "expense.report", scope="unit-a", amount=Decimal("10"))
    neighbor = Request("sam", "approve", "expense.report", scope="unit-b", amount=Decimal("10"))
    assert evaluate(policy, own).allowed
    assert not evaluate(policy, neighbor).allowed


def test_a_limit_caps_the_amount(policy):
    req = Request("sam", "approve", "expense.report", scope="unit-a", amount=Decimal("500.01"))
    assert not evaluate(policy, req).allowed


def test_a_limited_grant_with_no_amount_is_denied(policy):
    req = Request("sam", "approve", "expense.report", scope="unit-a")
    assert not evaluate(policy, req).allowed


# ---- the floor -------------------------------------------------------------------


@pytest.mark.parametrize("action,resource", [("release", "payment"), ("send", "message")])
def test_money_and_external_sends_happen_only_through_a_card(policy, action, resource):
    assert not evaluate(policy, Request("pat", action, resource)).allowed
    assert evaluate(policy, Request("pat", action, resource, via_card=True)).allowed


def test_an_agent_never_releases_money_whatever_it_is_granted(policy):
    decision = evaluate(policy, Request("mailroom", "release", "payment", via_card=True))
    assert not decision.allowed and "human" in decision.reason


def test_an_agent_that_reads_outside_documents_never_proposes_authority(policy):
    assert evaluate(policy, Request("admin", "propose", "authority")).allowed
    decision = evaluate(policy, Request("mailroom", "propose", "authority"))
    assert not decision.allowed and "outside" in decision.reason


def test_an_agent_never_confirms_authority(policy):
    decision = evaluate(policy, Request("mailroom", "confirm", "authority", proposer="admin"))
    assert not decision.allowed


def test_every_authority_decision_is_recorded(policy):
    assert evaluate(policy, Request("admin", "propose", "authority")).record
    assert evaluate(policy, Request("pat", "confirm", "authority", proposer="admin")).record


def test_a_change_record_names_who_proposed_and_who_confirmed():
    record = change_record(proposer="admin", confirmer="pat", before="a = 1\n", after="a = 2\n")
    assert record["proposer"] == "admin" and record["confirmer"] == "pat"
    assert record["before_sha256"] != record["after_sha256"]


# ---- who confirms an authority change -------------------------------------------


def test_second_person_confirm_refuses_the_proposer(policy):
    assert not evaluate(policy, Request("pat", "confirm", "authority", proposer="pat")).allowed
    assert evaluate(policy, Request("pat", "confirm", "authority", proposer="admin")).allowed


def test_self_confirm_lets_a_solo_person_confirm_their_own_change():
    solo = _with(authority_confirm="self")
    assert evaluate(solo, Request("pat", "confirm", "authority", proposer="pat")).allowed


def test_nobody_grants_what_they_do_not_hold(policy):
    assert may_grant(policy, "pat", parse_permission("release:payment"))
    assert not may_grant(policy, "sam", parse_permission("release:payment"))
    assert not may_grant(policy, "sam", parse_permission("approve:expense.report@any<=10"))
    assert not may_grant(policy, "sam", parse_permission("approve:expense.report@own<=1000"))
    assert not may_grant(policy, "sam", parse_permission("approve:expense.report@own"))
    assert may_grant(policy, "sam", parse_permission("approve:expense.report@own<=100"))


def test_a_change_is_checked_against_the_confirmer(policy):
    added = [parse_permission("release:payment")]
    assert check_change(policy, proposer="admin", confirmer="pat", added=added) == []
    problems = check_change(policy, proposer="admin", confirmer="sam", added=added)
    assert problems and "sam" in problems[0]
    assert check_change(policy, proposer="admin", confirmer="mailroom", added=[])


# ---- the safeguards (data, per shape) --------------------------------------------


def test_self_approval_never():
    p = _with(self_approval="never")
    assert not evaluate(p, Request("pat", "approve", "ap.invoice", submitter="pat")).allowed
    assert evaluate(p, Request("pat", "approve", "ap.invoice", submitter="sam")).allowed


def test_self_approval_below_a_limit():
    p = _with(self_approval="below_limit", self_approval_limit=500)
    under = Request("pat", "approve", "ap.invoice", submitter="pat", amount=Decimal("499"))
    over = Request("pat", "approve", "ap.invoice", submitter="pat", amount=Decimal("500"))
    assert evaluate(p, under).allowed
    assert not evaluate(p, over).allowed


def test_being_the_payee_counts_as_approving_your_own():
    p = _with(self_approval="never")
    assert not evaluate(p, Request("pat", "approve", "expense.report", payee="pat")).allowed


def test_solo_self_approval_carries_the_monthly_review_after():
    p = _with(self_approval="allowed", review_after="monthly")
    decision = evaluate(p, Request("pat", "approve", "expense.report", submitter="pat"))
    assert decision.allowed and decision.review_after


def test_a_second_approver_above_the_threshold():
    p = _with(second_approver_above=5000)
    small = Request("pat", "approve", "ap.invoice", submitter="sam", amount=Decimal("5000"))
    big = Request("pat", "approve", "ap.invoice", submitter="sam", amount=Decimal("5000.01"))
    assert not evaluate(p, small).needs_second
    assert evaluate(p, big).needs_second


@pytest.mark.parametrize(
    "mode,above,amount,allowed",
    [
        ("off", 0, "9999", True),
        ("always", 0, "1", False),
        ("above_limit", 500, "500", True),
        ("above_limit", 500, "500.01", False),
    ],
)
def test_the_approver_and_the_payer_differ_as_the_shape_says(mode, above, amount, allowed):
    p = _with(distinct_payer=mode, distinct_payer_above=above)
    req = Request(
        "pat", "release", "payment", approver="pat", amount=Decimal(amount), via_card=True
    )
    assert evaluate(p, req).allowed is allowed


@pytest.mark.parametrize("mode,amount,report", [("never", "9", False), ("always", "9", True)])
def test_self_approvals_are_reported_as_the_shape_says(mode, amount, report):
    p = _with(self_approval="allowed", report_self_approvals=mode)
    req = Request("pat", "approve", "ap.invoice", submitter="pat", amount=Decimal(amount))
    assert evaluate(p, req).report is report


# ---- every shape's defaults ------------------------------------------------------


def _rendered(shape: str):
    files = render("acme", "A", shape=shape, data_root_rel="acme-data")
    return parse_policy(tomllib.loads(files["authority.toml"]))


# The design's "Safeguards, by shape" table, as data.
EXPECTED_SAFEGUARDS = {
    "solo": {
        "self_approval": "allowed",
        "review_after": "monthly",
        "distinct_payer": "off",
        "authority_confirm": "self",
        "report_self_approvals": "never",
    },
    "small": {
        "self_approval": "below_limit",
        "review_after": "none",
        "distinct_payer": "above_limit",
        "authority_confirm": "second_person",
        "report_self_approvals": "above_limit",
    },
    "organization": {
        "self_approval": "never",
        "review_after": "none",
        "distinct_payer": "always",
        "authority_confirm": "group",
        "report_self_approvals": "always",
    },
}


@pytest.mark.parametrize("shape", SHAPES)
def test_each_shape_ships_the_safeguards_the_design_names(shape):
    safeguards = _rendered(shape).safeguards
    for key, value in EXPECTED_SAFEGUARDS[shape.split("-", 1)[1]].items():
        assert getattr(safeguards, key) == value, (shape, key)


@pytest.mark.parametrize("shape", SHAPES)
def test_each_shape_has_an_agent_that_may_propose_and_one_that_reads_outside(shape):
    policy = _rendered(shape)
    assert any(not a.reads_outside for a in policy.agents.values())
    assert any(a.reads_outside for a in policy.agents.values())


def test_nonprofit_shapes_scope_the_missionary_to_their_unit():
    for shape in ("nonprofit-small", "nonprofit-organization"):
        grants = _rendered(shape).roles["missionary"].grants
        assert grants and all(g.scope == "own" for g in grants), shape


def _principals_for(policy):
    """One person per role (scoped to unit u1), every agent the shape ships,
    and two agents a careless tenant over-granted with everything: one that
    reads outside documents and one that does not. The floor must hold for
    them too, because it lives in code and not in the grants."""
    everything = Role(grants=(parse_permission("*:*"),))
    over = {
        "rogue-outside": Principal("rogue-outside", "agent", ("everything",), (), True),
        "rogue-inside": Principal("rogue-inside", "agent", ("everything",), (), False),
    }
    policy = replace(
        policy,
        roles={**policy.roles, "everything": everything},
        agents={**policy.agents, **over},
    )
    people = {f"p-{name}": ([name], ["u1"]) for name in policy.roles}
    return policy.with_people(people)


AMOUNTS = (None, Decimal("100"), Decimal("1000"), Decimal("20000"))


@pytest.mark.parametrize("shape", SHAPES)
def test_the_floor_and_the_safeguards_hold_for_every_combination(shape):
    policy = _principals_for(_rendered(shape))
    sg = policy.safeguards
    principals = list(policy.people) + list(policy.agents)
    allowed: set[str] = set()
    for who, action, resource, scope, amount, self_side, via_card in itertools.product(
        principals, ACTIONS, RESOURCES, ("u1", "u2"), AMOUNTS, (True, False), (True, False)
    ):
        other = "someone-else"
        req = Request(
            who,
            action,
            resource,
            scope=scope,
            amount=amount,
            submitter=who if self_side else other,
            approver=who if self_side else other,
            proposer=who if self_side else other,
            via_card=via_card,
        )
        d = evaluate(policy, req)
        if not d.allowed:
            continue
        allowed.add(action)
        is_agent = who in policy.agents
        # floor 1: money and external sends only through a card; never an agent's money
        if action in ("release", "send"):
            assert via_card, req
        if action == "release":
            assert not is_agent, req
        # floor 2: agents never confirm; outside readers never propose
        if action == "confirm" and resource == "authority":
            assert not is_agent, req
            if self_side:
                assert sg.authority_confirm == "self", req
        if action == "propose" and resource == "authority" and is_agent:
            assert not policy.agents[who].reads_outside, req
        # floor 3: authority decisions are recorded
        if resource == "authority" and action in ("propose", "confirm"):
            assert d.record, req
        # no sideways movement: u2 is outside every enumerated person's unit
        if scope == "u2" and not is_agent:
            assert policy.holds_any_scope(who, action, resource), req
        # the shape's safeguards, as declared
        if action == "approve" and self_side:
            if sg.self_approval == "never":
                pytest.fail(f"self-approval allowed under 'never': {req}")
            if sg.self_approval == "below_limit":
                assert amount is not None and amount < sg.self_approval_limit, req
        if action == "release" and self_side and sg.distinct_payer == "always":
            pytest.fail(f"approver released under 'always': {req}")
    # The sweep proves something only if each guarded act is reachable in
    # this shape: a shape where everything is denied passes every assertion.
    assert {"view", "submit", "approve", "release", "send", "propose", "confirm"} <= allowed


def test_the_demo_carries_the_rendered_small_business_authority():
    assert load_kit(DEMO_DIR).authority is not None
    assert _rendered("commercial-small") == parse_policy(load_kit(DEMO_DIR).authority)


def test_the_kit_loader_runs_the_full_authority_check(tmp_path):
    (tmp_path / "authority.toml").write_text(
        '[money]\nout = "human"\n[roles.x]\ngrants = ["fly:*"]\n', encoding="utf-8"
    )
    with pytest.raises(KitError, match="fly"):
        load_kit(tmp_path)
