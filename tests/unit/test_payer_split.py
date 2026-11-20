"""One payer, some of each (board item t-payer-split; the owner, 2026-11-16).

"Someone who sends money every month may send some for the ministry work
and some as a personal gift. Tim's question gets a third answer, 'Some of
each', asks how much of each payment is for the ministry work, says both
parts back with their percentages, and splits every later deposit the same
way."

- The split is the person's, in their words: dollars of each payment, or a
  percentage. Code works out the other part and keeps the split on the payer
  as an exact fraction; no model picks it.
- Every deposit from that payer splits the same way, to the cent: the part
  for their ministry work rounds down to the cent and the gift takes the
  rest, so the two parts always add up to the deposit.
- The packet counts the ministry-work part as pay (Schedule C, no tax form)
  and the gift part as a personal gift, everywhere a payer's kind is read.
- A payer who is all pay or all gift packs exactly as before
  (test_payer_kinds_pinned.py)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from core.agents.expenses import packet, payers
from core.agents.expenses import statements as st
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_gift_or_pay import _monthly

ASK = "Do they send this because of your ministry work?"


def _split(share: str) -> payers.Payer:
    return payers.Payer.of(
        {
            "person": "daniel-reyes",
            "name": "Mom",
            "kind": payers.PERSONAL,
            "work": "some",
            "share": share,
            "words": "M PORTER",
        }  # fmt: skip
    )


# ---- the split, kept exactly ------------------------------------------------------------


def test_a_split_is_kept_on_the_payer_as_an_exact_fraction():
    mom = _split("3/4")
    assert mom.work == "some" and mom.share == Fraction(3, 4)
    assert mom.income and mom.split
    assert mom.label == payers.SOME_WORK


@pytest.mark.parametrize("share", ["", "0", "1", "0/4", "4/4", "5/4", "-1/4", "a/b", "1/0"])
def test_a_split_that_isnt_part_of_each_payment_is_no_split(share):
    mom = _split(share)
    assert mom.work == "" and mom.share is None and not mom.split
    assert not mom.income  # still a gift, still asked


def test_only_a_gift_can_be_split():
    church = payers.Payer.of(
        {"person": "d", "name": "Faith", "kind": payers.CHURCH, "work": "some", "share": "1/2"}
    )
    assert church.work == "" and church.share is None


@pytest.mark.parametrize(
    ("share", "dollars", "work", "gift"),
    [
        ("3/4", "200.00", "150.00", "50.00"),
        ("1/3", "100.00", "33.33", "66.67"),  # the work part rounds down; the gift takes the cent
        ("2/3", "100.00", "66.66", "33.34"),
        ("1/2", "0.01", "0.00", "0.01"),
        ("2/5", "500.00", "200.00", "300.00"),
    ],
)
def test_each_deposit_splits_to_the_cent(share, dollars, work, gift):
    assert _split(share).parts(dollars) == (work, gift)


def test_the_two_parts_always_add_up_to_the_deposit():
    for share in ("1/3", "2/7", "5/9", "37/100", "15000/20001"):
        mom = _split(share)
        for cents in range(1, 3000, 7):
            dollars = f"{Decimal(cents) / 100:.2f}"
            work, gift = mom.parts(dollars)
            assert Decimal(work) + Decimal(gift) == Decimal(dollars)
            assert int(Decimal(work) * 100) == cents * Fraction(share) // 1  # rounds down


# ---- through the door: the third answer -------------------------------------------------


def _mom(solo):  # noqa: F811
    door = _monthly(solo)  # M PORTER sends $500.00 on Jun 1, Jul 1, Aug 3
    put = door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER"})
    assert put["status"] == "set", put
    return door


def test_some_of_each_in_dollars_says_both_parts_back_with_their_percentages(solo):  # noqa: F811
    door = _mom(solo)
    put = door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "$150"})
    assert put["status"] == "set", put
    assert put["say"] == (
        "Done: of each $500.00 from Mom, $150.00 (30%) counts as pay for their ministry work, "
        "on Schedule C of their tax packet, and $350.00 (70%) stays a personal gift, kept off "
        "their taxes. Every deposit from Mom splits the same way."
    )


def test_some_of_each_as_a_percentage(solo):  # noqa: F811
    door = _mom(solo)
    put = door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "40%"})
    assert put["status"] == "set", put
    assert "$200.00 (40%)" in put["say"] and "$300.00 (60%)" in put["say"]


def test_a_third_of_each_payment_says_percentages_that_add_up(solo):  # noqa: F811
    door = _mom(solo)
    put = door.call(
        "keep_payers",
        {"payer": "Mom", "ministry_work": "some", "work_part": "$100", "of_each": "$300"},
    )
    assert put["status"] == "set", put
    assert "of each $300.00 from Mom, $100.00 (33.33%)" in put["say"]
    assert "$200.00 (66.67%)" in put["say"]


def test_the_packet_counts_the_work_part_as_pay_and_the_rest_as_a_gift(solo):  # noqa: F811
    door = _mom(solo)
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "$150"})
    got = door.call("tax_packet", {"year": "2026"})
    [mom] = [p for p in got["payers"] if p["name"] == "Mom"]
    assert mom["kind"] == payers.SOME_WORK and mom["form"] == ""
    assert mom["came_in_seen"] == "450.00"
    assert mom["work_share"] == "30%"
    assert [d["dollars"] for d in mom["deposits"]] == ["150.00"] * 3
    from_mom = [g for g in got["personal_gifts"] if g["from"] == "Mom"]
    assert [g["dollars"] for g in from_mom] == ["350.00"] * 3
    assert all(g["of"] == "500.00" for g in from_mom)
    assert got["schedule_c"]["income"] == "450.00"
    assert got["schedule_c"]["with_no_form"] == "450.00"
    assert not [a for a in got["asks"] if a.get("payer") == "Mom"]  # answered, asked no more
    assert (
        "Mom sent $1,500.00 that Tim saw arrive. Of each payment, 30%, $450.00 in all, is pay "
        "for their ministry work, and the rest stays a personal gift." in got["says"]
    )


def test_the_packet_page_names_the_part_for_their_ministry_work(solo):  # noqa: F811
    door = _mom(solo)
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "30%"})
    blocks = packet.page(door.call("tax_packet", {"year": "2026"}))
    [income] = [b for b in blocks if b[0] == "table" and b[1] == "Income from each payer"]
    assert (
        "Mom: 30% of each payment, for their ministry work, seen arriving in the bank",
        "$450.00",
        True,
    ) in income[2]
    assert any("$1,050.00 from 1 giver" in str(b) for b in blocks)


def test_the_split_stays_when_the_payer_is_said_again(solo):  # noqa: F811
    door = _mom(solo)
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "30%"})
    door.call("keep_payers", {"payer": "Mom", "kind": "gift", "words": "M PORTER;PORTER"})
    got = door.call("tax_packet", {"year": "2026"})
    [mom] = [p for p in got["payers"] if p["name"] == "Mom"]
    assert mom["came_in_seen"] == "450.00"


def test_a_yes_or_a_no_after_a_split_undoes_it(solo):  # noqa: F811
    door = _mom(solo)
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "30%"})
    door.call("keep_payers", {"payer": "Mom", "ministry_work": "no"})
    got = door.call("tax_packet", {"year": "2026"})
    assert "Mom" not in [p["name"] for p in got["payers"]]
    assert [g["dollars"] for g in got["personal_gifts"] if g["from"] == "Mom"] == ["500.00"] * 3


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ({}, "How much of each payment from Mom is for their ministry work?"),
        ({"work_part": "lots"}, "How much of each payment from Mom is for their ministry work?"),
        ({"work_part": "0%"}, "say no"),
        ({"work_part": "100%"}, "say yes"),
        ({"work_part": "$500"}, "say yes"),
        ({"work_part": "$600"}, "more than each payment"),
        ({"work_part": "$150", "of_each": "nothing"}, "Say the payment in dollars"),
    ],
)
def test_a_split_tim_cant_keep_as_said_is_asked_again(solo, args, words):  # noqa: F811
    door = _mom(solo)
    put = door.call("keep_payers", {"payer": "Mom", "ministry_work": "some", **args})
    assert put["status"] == "needs" and words in put["say"], put
    got = door.call("tax_packet", {"year": "2026"})
    assert {"payer": "Mom", "ask": ASK} in got["asks"]  # nothing was kept


def test_dollars_with_no_payment_seen_need_the_payment(solo):  # noqa: F811
    door = _monthly(solo)
    door.call("keep_payers", {"payer": "Aunt Ruth", "kind": "gift"})
    put = door.call(
        "keep_payers", {"payer": "Aunt Ruth", "ministry_work": "some", "work_part": "$50"}
    )
    assert put["status"] == "needs" and "of each" in put["say"]
    put = door.call(
        "keep_payers",
        {"payer": "Aunt Ruth", "ministry_work": "some", "work_part": "$50", "of_each": "$200"},
    )
    assert put["status"] == "set" and "$50.00 (25%)" in put["say"], put


# ---- every place a payer's kind is read -------------------------------------------------


def test_a_ministry_deposit_from_a_split_payer_is_asked_as_some_of_each():
    mom = _split("3/10")
    rows = [
        {"date": "2026-08-03", "dollars": "500.00", "words": "ZELLE M PORTER", "role": st.CAME_IN}
    ]
    got = payers.sort(rows, [mom])
    [ask] = got.asks
    assert ask["ask"].endswith("Was it part their pay and part a gift for them?")


def test_a_split_payer_is_never_taken_for_the_ministrys_own_pay():
    mom = payers.Payer.of(
        {
            "person": "d",
            "name": "Living Water Ministry",
            "kind": payers.PERSONAL,
            "work": "some",
            "share": "1/2",
        }  # fmt: skip
    )
    pay = ("Living Water Ministry", [{"date": "2026-08-01", "dollars": "100.00"}])
    listed, _got = payers.sort_with_pay([], [mom], pay, "d")
    assert len(listed) == 2 and listed[1].kind == payers.CHURCH


def test_the_packet_rows_split_too():
    mom = _split("3/10")
    rows = [
        {"date": d, "dollars": "500.00", "words": "ZELLE M PORTER", "role": st.THEIRS_IN}
        for d in ("2026-06-01", "2026-07-01")
    ]
    got = payers.sort(rows, [mom])
    out, says = payers.rows([mom], got, {}, {}, ("daniel-reyes", "2026"))
    assert out[0]["came_in_seen"] == "300.00" and out[0]["work_share"] == "30%"
    assert [g["dollars"] for g in got.gifts] == ["350.00", "350.00"]
    assert "Of each payment, 30%, $300.00 in all, is pay" in says[0]


# ---- the words ------------------------------------------------------------------------


def test_the_split_words_carry_no_machinery_and_no_em_dash():
    from core.tools import tax

    for key, words in tax.SPLIT_WORDS.items():
        assert "—" not in words, key
        for banned in ("card", "tool", "ledger", "tenant", "_", "#", "fraction", "delve"):
            assert banned not in words.lower(), (key, banned)


def test_the_answer_is_in_the_faq():
    from core.capabilities.faq import load_faq

    [entry] = [e for e in load_faq() if e["id"] == "some-of-each"]
    assert "keep_payers" in entry["covers"] and "rounds down" in entry["answer"]


def test_her_card_on_tims_page_shows_the_split_she_said():
    from core.talk import hold

    assert hold.kind(__import__("core.tools.tax", fromlist=["KEEP_PAYERS"]).KEEP_PAYERS) == (
        hold.DECISION
    )  # still held for her tap
    line = hold.summary(
        "keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "30%"}, you="d"
    )
    assert line == "Keep Mom on your list of who pays: 30%"
    line = hold.summary(
        "keep_payers", {"payer": "Mom", "ministry_work": "some", "work_part": "150"}, you="d"
    )
    assert line == "Keep Mom on your list of who pays: $150.00"
