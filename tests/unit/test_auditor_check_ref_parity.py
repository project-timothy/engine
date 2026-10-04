"""Tripwire: the auditor's check-reference normalizer must agree with the
engine's.

Lens 15's fourth exit (2026-10-02) closes an unknown clearing when the owner
answered that same physical check on a sweep card, and the join is the pair
the ENGINE calls one check: ``(norm_check_ref(ref), amount_cents)``, the
identity ``_carded_checks`` uses when it declines to card a check another
lane already carries. The independence lint forbids importing core under
``auditor/``, so the auditor re-spells the normalizer; if the two ever
disagree, the engine stops carding a check the auditor still warns about, or
the auditor closes a finding the owner was never asked about.

This test lives on the ENGINE side of the boundary, where importing both is
allowed. It is one-directional in neither sense: the two functions must agree
on every input, so the comparison is equality.
"""

from __future__ import annotations

import pytest

from auditor.lenses.reconcile import _norm_check_ref
from core.agents.ap.reconcile import norm_check_ref

# The spellings this ledger actually carries, plus the shapes the contract
# names: a bare bank number, every prefix the engine strips, punctuation and
# spacing a hand-written note adds, and the degenerate inputs.
REFS = [
    "8158",
    "Check 8158",
    "check8158",
    "CHECK #8158",
    "ck 8158",
    "chk. 8158",
    "no 8158",
    "num 8158",
    "  8158  ",
    "8158-A",
    "DP-Purchase-9013",
    "700000391",
    "EXP-2",
    "",
    "   ",
    "a messy free-text reference",
]


@pytest.mark.parametrize("ref", REFS)
def test_the_auditors_check_ref_normalizer_matches_the_engines(ref):
    assert _norm_check_ref(ref) == norm_check_ref(ref), (
        "auditor/lenses/reconcile.py's _norm_check_ref drifted from "
        "core/agents/ap/reconcile.py's norm_check_ref; the sweep-card join in "
        "lens 15 and the engine's own _carded_checks must key one physical "
        "check identically"
    )


def test_the_auditors_normalizer_tolerates_a_non_string_payload():
    """The lens feeds it whatever the event's JSON held, which is not always a
    string (a bank number parsed as a number, or a missing key)."""
    assert _norm_check_ref(None) == ""
    assert _norm_check_ref(8158) == "8158"
