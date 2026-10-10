"""Routes, the clock and delegation, as pure logic (#436;
docs/tenant-kit-design.md section 3, "Routing: one document, on a clock")."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.authority import AuthorityError, CardRule, Request, parse_policy
from core.authority.routes import Step
from core.authority.routing import (
    CardRoute,
    Delegation,
    check_delegation,
    decide,
    due_date,
    level_due,
    route_for,
    waiting_on,
)

PAY = CardRule("approve", "payment", amount="amount")
TODAY = date(2026, 10, 8)


def _policy(routing: dict | None = None, **safeguards) -> object:
    return parse_policy(
        {
            "money": {"out": "human"},
            "safeguards": {"self_approval": "never", **safeguards},
            "roles": {
                "owner": {"grants": ["*:*"]},
                "approver": {"grants": ["view:*", "approve:*"]},
                "board": {"grants": ["view:*", "approve:*"]},
                "viewer": {"grants": ["view:*"]},
            },
            "people": {
                "dana": {"roles": ["owner"], "backup": "sam"},
                "sam": {"roles": ["approver"]},
                "lee": {"roles": ["approver"]},
                "kim": {"roles": ["board"]},
                "vic": {"roles": ["viewer"]},
            },
            "agents": {},
            "routing": routing or {},
        }
    )


def _req(who: str, amount: str = "100", submitter: str = "") -> Request:
    return Request(
        who, "approve", "payment", amount=Decimal(amount), submitter=submitter, via_card=True
    )


def _decide(policy, route, who, decision="approved", delegations=(), **req):
    return decide(
        policy,
        PAY,
        _req(who, **req),
        route,
        decision=decision,
        delegations=list(delegations),
        today=TODAY,
        now="2026-10-08T12:00:00+00:00",
    )


# ---- the file -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "routing, match",
    [
        ({"route": [{"resource": "rocket", "steps": [{}]}]}, "resource"),
        ({"route": [{"steps": [{"role": "ghost"}]}]}, "ghost"),
        ({"route": [{"steps": [{"need": "three"}]}]}, "need"),
        ({"route": [{"steps": [{"need": "all"}]}]}, "names a role"),
        ({"route": [{"steps": []}]}, "at least one step"),
        ({"upward": True}, "upward_role"),
        ({"owner": "nobody"}, "owner"),
        ({"window_days": 3, "second_reminder_days": 4}, "window_days"),
        ({"backup": "yes"}, "true or false"),
    ],
)
def test_the_routing_table_refuses_what_it_cannot_hold(routing, match):
    with pytest.raises(AuthorityError, match=match):
        _policy(routing)


def test_a_backup_is_another_person():
    with pytest.raises(AuthorityError, match="backup"):
        parse_policy(
            {
                "money": {"out": "human"},
                "roles": {"r": {"grants": []}},
                "people": {"a": {"roles": ["r"], "backup": "a"}},
            }
        )


# ---- which route ----------------------------------------------------------------------


def test_no_route_is_one_step_and_two_people_past_the_line():
    policy = _policy(second_approver_above=5000)
    assert route_for(policy, PAY, Decimal("100")) == (Step(),)
    assert route_for(policy, PAY, Decimal("6000")) == (Step(need="two"),)
    assert route_for(policy, PAY, None) == (Step(need="two"),)


def test_the_most_specific_route_wins():
    policy = _policy(
        {
            "route": [
                {"resource": "*", "steps": [{"role": "approver"}]},
                {"resource": "payment", "steps": [{"role": "approver"}, {"role": "owner"}]},
                {
                    "resource": "payment",
                    "above": 10000,
                    "steps": [{"role": "board", "need": "all"}],
                },
            ]
        }
    )
    assert [s.role for s in route_for(policy, PAY, Decimal("50"))] == ["approver", "owner"]
    assert route_for(policy, PAY, Decimal("20000")) == (Step("board", "all"),)
    other = CardRule("approve", "books", money=False)
    assert route_for(policy, other, Decimal("0")) == (Step("approver"),)


def test_a_one_step_route_past_the_line_gains_a_second_approver():
    policy = _policy({"route": [{"steps": [{"role": "approver"}]}]}, second_approver_above=500)
    assert route_for(policy, PAY, Decimal("900")) == (Step("approver"), Step())


# ---- deciding along it -------------------------------------------------------------------


def test_a_serial_route_resolves_after_its_last_step():
    policy = _policy({"route": [{"steps": [{"role": "approver"}, {"role": "owner"}]}]})
    route = CardRoute(steps=route_for(policy, PAY, Decimal("100")), opened="2026-10-01")
    first = _decide(policy, route, "sam")
    assert not first.final and first.route.index == 1
    assert _decide(policy, first.route, "lee").refusal  # lee holds approver, not owner
    last = _decide(policy, first.route, "dana")
    assert last.final


def test_all_means_every_holder():
    policy = _policy({"route": [{"steps": [{"role": "approver", "need": "all"}]}]})
    route = CardRoute(steps=route_for(policy, PAY, Decimal("100")), opened="2026-10-01")
    one = _decide(policy, route, "sam")
    assert not one.final
    assert _decide(policy, one.route, "lee").final


def test_any_one_no_ends_the_route():
    policy = _policy({"route": [{"steps": [{"role": "approver"}, {"role": "owner"}]}]})
    route = CardRoute(steps=route_for(policy, PAY, Decimal("100")), opened="2026-10-01")
    assert _decide(policy, route, "sam", decision="rejected").final


def test_a_viewer_cannot_vote():
    policy = _policy()
    route = CardRoute(steps=(Step(),), opened="2026-10-01")
    assert "no grant" in _decide(policy, route, "vic").refusal


# ---- delegation -----------------------------------------------------------------------


def _d(frm="sam", to="vic", role="approver", start=TODAY, until=date(2026, 10, 20)):
    return Delegation(frm, to, role, start, until)


@pytest.mark.parametrize(
    "d, match",
    [
        (_d(frm="ghost"), "only a person delegates"),
        (_d(to="ghost"), "goes to a person"),
        (_d(to="sam"), "someone else"),
        (_d(role="owner"), "directly"),
        (_d(until=date(2026, 10, 1), start=date(2026, 9, 1)), "still to come"),
        (_d(until=date(2027, 3, 1)), "at most"),
    ],
)
def test_a_delegation_follows_the_rules(d, match):
    assert match in check_delegation(_policy(), d, TODAY)


def test_a_good_delegation_passes():
    assert check_delegation(_policy(), _d(), TODAY) == ""


def test_a_delegate_acts_on_the_delegators_behalf_until_the_date():
    policy = _policy({"route": [{"steps": [{"role": "approver"}]}]})
    route = CardRoute(steps=(Step("approver"),), opened="2026-10-01")
    out = _decide(policy, route, "vic", delegations=[_d()])
    assert out.final and out.authority == "sam"
    lapsed = _d(start=date(2026, 9, 1), until=date(2026, 10, 7))
    assert _decide(policy, route, "vic", delegations=[lapsed]).refusal


def test_no_delegation_of_a_delegation():
    policy = _policy({"route": [{"steps": [{"role": "approver"}]}]})
    route = CardRoute(steps=(Step("approver"),), opened="2026-10-01")
    chain = [_d(), _d(frm="vic", to="kim")]
    assert _decide(policy, route, "kim", delegations=chain).refusal


def test_a_delegate_never_decides_their_own_submission():
    policy = _policy({"route": [{"steps": [{"role": "approver"}]}]})
    route = CardRoute(steps=(Step("approver"),), opened="2026-10-01")
    out = _decide(policy, route, "vic", delegations=[_d()], submitter="vic")
    assert "own submission" in out.refusal


# ---- who it waits on --------------------------------------------------------------------


def test_it_waits_on_the_role_holders_or_their_delegates_or_the_handoff():
    policy = _policy({"route": [{"steps": [{"role": "approver"}]}]})
    route = CardRoute(steps=(Step("approver"),), opened="2026-10-01")
    assert waiting_on(policy, PAY, route, [], TODAY) == ["sam", "lee"]
    assert waiting_on(policy, PAY, route, [_d()], TODAY) == ["vic", "lee"]
    handed = CardRoute(steps=(Step("approver"),), opened="2026-10-01", handoff_to="lee")
    assert waiting_on(policy, PAY, handed, [], TODAY) == ["lee"]


# ---- the clock --------------------------------------------------------------------------


def _route(opened="2026-10-01", **kw):
    return CardRoute(steps=(Step(),), opened=opened, **kw)


def test_the_clock_is_the_documents_deadline_else_the_window():
    policy = _policy()
    assert due_date(_route(), {}, policy.routing) == date(2026, 10, 6)
    assert due_date(_route(), {"due_date": "2026-10-30"}, policy.routing) == date(2026, 10, 30)


def test_reminders_climb_on_the_window():
    r = _policy().routing  # window 5, first 2, second 4
    assert level_due(_route(), {}, r, date(2026, 10, 2), "commercial-small") == 0
    assert level_due(_route(), {}, r, date(2026, 10, 3), "commercial-small") == 1
    assert level_due(_route(), {}, r, date(2026, 10, 5), "commercial-small") == 2
    assert level_due(_route(), {}, r, date(2026, 10, 9), "commercial-small") == 2  # no backup


def test_a_far_deadline_means_no_early_nagging():
    r = _policy().routing
    params = {"due_date": "2026-10-30"}
    assert level_due(_route(), params, r, date(2026, 10, 20), "commercial-small") == 0
    assert level_due(_route(), params, r, date(2026, 10, 27), "commercial-small") == 1


def test_the_backup_only_when_turned_on():
    r = _policy({"backup": True}).routing
    assert level_due(_route(), {}, r, date(2026, 10, 6), "commercial-small") == 3


def test_upward_only_in_the_organization_shape():
    r = _policy({"backup": True, "upward": True, "upward_role": "board"}).routing
    assert level_due(_route(), {}, r, date(2026, 10, 6), "commercial-small") == 3
    assert level_due(_route(), {}, r, date(2026, 10, 6), "nonprofit-organization") == 4


def test_on_it_pauses_the_clock_until_its_date():
    r = _policy().routing
    paused = _route(on_it_until="2026-10-09", on_it_set="2026-10-05")
    assert level_due(paused, {}, r, date(2026, 10, 7), "commercial-small") == 0
    assert due_date(paused, {}, r) == date(2026, 10, 10)
    assert level_due(paused, {}, r, date(2026, 10, 9), "commercial-small") == 2
