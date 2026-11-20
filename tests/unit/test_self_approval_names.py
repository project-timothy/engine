"""Whose card it is, by name (review 2026-11-10, the owner's decision).

A card's person was matched to the decider by the slug of the name, so a
person whose id in [people] is not that slug ("sam" for "Sam Staffer")
approved their own card unseen. Each [people] entry may now carry its
``name``; a card matches its id or that name. A card whose person matches
nobody fails closed: it may be the decider's own unless the decider's
entry says their name and it is another."""

from __future__ import annotations

import pytest

from core.agents.policy.jobs import ASK_CARD, CARD_AUTHORITY
from core.authority import AuthorityError, card_request, evaluate, parse_policy

RULE = CARD_AUTHORITY[ASK_CARD]


def _policy(**people: dict) -> object:
    return parse_policy(
        {
            "money": {"out": "human"},
            "roles": {
                "reviewer": {"grants": ["approve:*"]},
                "missionary": {"grants": ["submit:*@own"]},
            },
            "people": {
                "ruth-hollis": {"roles": ["missionary"], "scopes": ["ruth-hollis"]},
                **people,
            },
        }
    )


def _card(person: str) -> dict:
    return {"person": person, "amount": "300.00", "approver_role": ""}


def _decides(policy, who: str, person: str):
    return evaluate(
        policy, card_request(rule=RULE, principal=who, params=_card(person), policy=policy)
    )


def test_a_named_person_never_approves_their_own_card():
    policy = _policy(sam={"roles": ["reviewer"], "name": "Sam Staffer"})
    refused = _decides(policy, "sam", "Sam Staffer")
    assert not refused.allowed and "own" in refused.reason
    assert not _decides(policy, "sam", "sam").allowed  # the id still names them


def test_a_card_matching_nobody_is_possibly_the_unnamed_deciders_own():
    policy = _policy(sam={"roles": ["reviewer"]})
    refused = _decides(policy, "sam", "Sam Staffer")
    assert not refused.allowed
    assert "name" in refused.reason and "people" in refused.reason


def test_a_decider_whose_name_is_known_decides_a_card_matching_nobody():
    policy = _policy(carol={"roles": ["reviewer"], "name": "Carol Jennings"})
    assert _decides(policy, "carol", "Volunteer Without Sign-In").allowed


def test_a_card_matched_to_someone_else_is_decided_as_always():
    policy = _policy(sam={"roles": ["reviewer"]})
    assert _decides(policy, "sam", "Ruth Hollis").allowed  # her id is the slug of her name


def test_a_named_entry_still_reaches_its_own_unit():
    policy = _policy(
        mei={"roles": ["missionary"], "scopes": ["mei"], "name": "Mei Tan"},
        carol={"roles": ["reviewer"], "name": "Carol Jennings"},
    )
    request = card_request(rule=RULE, principal="carol", params=_card("Mei Tan"), policy=policy)
    assert request.submitter == "mei"
    assert evaluate(policy, request).allowed


def test_a_name_must_be_words():
    with pytest.raises(AuthorityError):
        _policy(sam={"roles": ["reviewer"], "name": 7})
