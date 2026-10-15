"""What a person reads through the box's door is in plain words (the true
north: the complexity is real and the computer hides it).

The engine's refusal reasons are exact and stay that way at the terminal,
where an operator reads them. Through the door they reach a missionary or a
volunteer treasurer, so the door says what happened and what to do, with no
ids, resource names, file names or rule words. A reason the door does not
know still comes out plain. The Approve page reads the card the same way.
"""

from __future__ import annotations

import re

import pytest

from core.tools.decide import SUMMARY_CAP, plain_refusal, plain_words

MACHINERY = re.compile(
    r"[a-z]+\.[a-z_]+|\b[a-z]+-[a-z]+\b|\bgrant\b|\bforbid\b|\bterminal\b|\bwitness\b|"
    r"\btenant\b|\btoml\b|\bscope\b|\bpermission\b|\bledger\b|\[|invariant|approval #",
    re.IGNORECASE,
)

ROW = {
    "id": 7,
    "agent": "expenses",
    "action_type": "expenses.report_review",
    "params": {"person": "Ruth Hollis", "total_cents": "18640"},
}

REASONS = [
    "no grant lets ruth-hollis approve expense.report here",
    "forbid 'approve:payment' wins",
    "'ruth-hollis' is not a person or agent in authority.toml",
    "this tenant does not let anyone approve their own",
    "approving your own is allowed only below 100",
    "this step waits on board, which ruth-hollis does not hold",
    "a delegate never decides their own submission",
    "carol-jennings already approved this card; the next yes is someone else's",
    "expense.report is decided at a terminal: authority.toml's [presence] door does not open it "
    "to a witnessed yes",
    "the witness is ruth-hollis's, not carol-jennings's",
    "the witness is for card 8, not #7",
    "the witness says reject, not approve",
    "carol-jennings vouched on this card, so does not also decide it",
    "don-pruitt cannot vouch on their own card",
    "don-pruitt already voted on this card, so cannot also vouch",
    "approval #7 (expenses.report_review) is a human-only card: carol-jennings decides it with "
    "their own Face ID or at a terminal, never by a vouched yes",
    "approval #7 (expenses.report_review) refused for ruth-hollis: no grant lets ruth-hollis "
    "approve expense.report here",
    "something the door has never seen before, from core/authority",
]


@pytest.mark.parametrize("reason", REASONS)
def test_every_refusal_reads_in_plain_words(reason):
    said = plain_refusal(reason, ROW, "approve")
    assert said and not MACHINERY.search(said), said


def test_a_refusal_says_what_it_is_about():
    said = plain_refusal("this tenant does not let anyone approve their own", ROW, "approve")
    assert "your own expense report" in said


def test_the_approve_page_reads_the_card_in_plain_words():
    # No "(card #7)": on the real-phone check (2026-10-10) the number read as
    # noise to the treasurer; the signed challenge binds the card id anyway.
    assert plain_words(ROW, "approve") == "Approve an expense report for Ruth Hollis, $186.40."
    assert plain_words(ROW, "reject").startswith("Reject an expense report")


def test_a_bill_names_its_vendor():
    row = {
        "id": 3,
        "agent": "ap",
        "action_type": "ap.payment_recommendation",
        "params": {"vendor": "Marion Roofing", "amount": "3000.00"},
    }
    assert plain_words(row, "approve") == "Approve a payment to Marion Roofing, $3,000.00."


MONTH = {
    "id": 9,
    "agent": "expenses",
    "action_type": "expenses.report_review",
    "params": {
        "person": "Ruth Hollis",
        "month": "2026-09",
        "total_cents": "5410",
        "lines": "2026-09-03: $42.10, Meals; 2026-09-15: $12.00, Fuel, Kathmandu Field; "
        "2026-09-09: $28.21 (NPR 4,300.00 at 152.45 per dollar on 2026-09-09), Supplies",
    },
}


def test_the_approve_page_shows_each_receipt_under_the_card():
    # The owner, 2026-10-14 (usability walk call 3): the approval page named the
    # total but not what was in it. The lines ride in the signed summary, so
    # what the person read is what their Face ID said yes to.
    said = plain_words(MONTH, "approve")
    head, *lines = said.split("\n")
    assert head.startswith("Approve an expense report for Ruth Hollis")
    assert lines == [
        "Sep 3: $42.10, Meals",
        "Sep 15: $12.00, Fuel, Kathmandu Field",
        "Sep 9: $28.21 (4,300 Nepali rupees at 152.45 per dollar on Sep 9), Supplies",
    ]
    assert not MACHINERY.search(said), said


def test_a_long_month_stops_at_the_cap_and_says_how_many_more():
    many = "; ".join(
        f"2026-09-{n % 30 + 1:02d}: $1{n}.00, Meals at the guest house by the school"
        for n in range(60)
    )
    row = {**MONTH, "params": {**MONTH["params"], "lines": many}}
    said = plain_words(row, "approve")
    assert len(said) <= SUMMARY_CAP
    assert said.endswith("more receipts.")
    shown = said.count("\n") - 1
    assert said.endswith(f"and {60 - shown} more receipts.")


def test_a_card_with_no_lines_reads_as_one_line():
    assert "\n" not in plain_words(ROW, "approve")


def test_a_currency_reads_by_its_name_and_an_unknown_code_stays():
    from core.tools.receipts import currency_name

    assert currency_name("NPR") == "Nepali rupees"
    assert currency_name("ZMW") == "Zambian kwacha"
    assert currency_name("XYZ") == ""
    row = {**MONTH, "params": {**MONTH["params"], "lines": "2026-09-04: $9.00 (EUR 8.31 at 0.92)"}}
    assert plain_words(row, "approve").endswith("Sep 4: $9.00 (EUR 8.31 at 0.92)")
