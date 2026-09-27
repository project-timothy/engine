"""Unit tests for the AP status vocabulary and transition rules.

The "Scheduled in bill pay" state between Received and Paid is the load-bearing
addition: its absence nearly caused a ~$38K double-payment (2026-05-21).
"""

from __future__ import annotations

import pytest

from core.agents.ap.status import (
    COMMITTED_STATUSES,
    PAYABLE_STATUSES,
    SETTLED_STATUSES,
    InvalidTransition,
    assert_transition,
    is_committed,
    is_payable_eligible,
    is_settled,
)


def test_vocabulary_sets_are_disjoint_and_complete():
    assert set() == PAYABLE_STATUSES & COMMITTED_STATUSES
    assert set() == COMMITTED_STATUSES & SETTLED_STATUSES
    assert set() == PAYABLE_STATUSES & SETTLED_STATUSES
    assert "Scheduled in bill pay" in COMMITTED_STATUSES
    assert "Scheduled" in COMMITTED_STATUSES
    assert {"Received", "Approved", "Outstanding"} <= PAYABLE_STATUSES
    assert "Paid" in SETTLED_STATUSES


def test_committed_money_is_never_payable():
    assert is_committed("Scheduled in bill pay")
    assert not is_payable_eligible("Scheduled in bill pay")
    assert is_settled("Paid")
    assert not is_payable_eligible("Paid")
    assert is_payable_eligible("Received")


def test_the_canonical_flow_is_legal():
    assert_transition("Received", "Scheduled in bill pay")
    assert_transition("Scheduled in bill pay", "Paid")
    assert_transition("Received", "Approved")
    assert_transition("Approved", "Scheduled in bill pay")
    assert_transition("Scheduled", "Paid")
    # Voiding a payable item is legal.
    assert_transition("Received", "Void - Duplicate")


def test_backward_and_nonsense_transitions_raise():
    with pytest.raises(InvalidTransition):
        assert_transition("Paid", "Received")  # settled never reopens silently
    with pytest.raises(InvalidTransition):
        assert_transition("Scheduled in bill pay", "Received")  # committed never reverts
    with pytest.raises(InvalidTransition):
        assert_transition("Received", "NotAStatus")
