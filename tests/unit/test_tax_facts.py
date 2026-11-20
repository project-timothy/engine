"""What only the person tells Tim for their taxes (board chunk c38; the
11/15 plan, Session 3 PR 1): filing status, a spouse's wages and tax
withheld as one pair, last year's total tax, estimates paid, and trips.
The person alone says and reads these, and shares their packet with whom
they choose (the owner, 2026-11-15, decision 2)."""

from __future__ import annotations

from core.agents.expenses import tax_facts as tf
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door


def _daniel(solo):  # noqa: F811
    return _door(solo, "daniel-reyes")  # today is 2026-10-13


def _said(door) -> dict:
    return door.call("tax_packet", {"year": "2026"})["what_they_said"]


# ---- the words ---------------------------------------------------------------------------


def test_a_filing_status_is_read_from_plain_words():
    for said in ("single", "Single", "not married"):
        assert tf.status_of(said) == tf.SINGLE
    for said in ("married filing jointly", "married, filing jointly", "MFJ", "joint", "married"):
        assert tf.status_of(said.replace(",", "")) == tf.JOINT
    for said in ("married filing separately", "head of household"):
        assert tf.status_of(said) == "other"
    assert tf.status_of("it's complicated") == ""


def test_the_newest_fact_stands_field_by_field():
    said = [
        {"person": "p", "year": "2026", "status": tf.SINGLE, "last_year_tax": "100"},
        {"person": "p", "year": "2026", "status": tf.JOINT},
        {"person": "p", "year": "2025", "status": tf.SINGLE},
        {"person": "p", "year": "2026", "estimate_day": "2026-04-15", "estimate_cents": "5000"},
        {"person": "p", "year": "2026", "estimate_day": "2026-06-15", "estimate_cents": "7000"},
        {"person": "p", "year": "2026", "estimate_day": "2026-04-15", "estimate_cents": "0"},
    ]
    got = tf.facts(said, "p", "2026")
    assert got.status == tf.JOINT and got.last_year_tax == 100
    assert got.estimates_paid == (("2026-06-15", 7000),)


def test_a_trip_said_again_by_its_day_left_replaces_it_and_forget_drops_it():
    said = [
        {"person": "p", "left": "2026-01-08", "back": "2026-05-01", "country": "Nepal"},
        {"person": "p", "left": "2026-01-08", "back": "2026-06-02", "country": "Nepal"},
        {"person": "p", "left": "2026-07-01", "country": "India"},
        {"person": "p", "left": "2026-07-01", "forget": "yes"},
    ]
    assert tf.trips(said, "p") == [tf.Trip("2026-01-08", "2026-06-02", "Nepal")]


# ---- keep_tax_facts ----------------------------------------------------------------------


def test_the_filing_status_is_kept_and_said_back(solo):  # noqa: F811
    door = _daniel(solo)
    put = door.call("keep_tax_facts", {"status": "single"})
    put.pop("next")  # the next thing ends every door answer (chunk 1); tested in test_next_thing
    assert put == {"status": "set", "say": "Done: Tim has filing single for 2026."}
    assert _said(door)["filing_status"] == "single"


def test_a_status_tim_leaves_to_the_preparer_keeps_nothing(solo):  # noqa: F811
    door = _daniel(solo)
    put = door.call("keep_tax_facts", {"status": "married filing separately"})
    assert put["status"] == "needs" and "preparer" in put["say"]
    assert _said(door)["filing_status"] == ""


def test_a_spouses_wages_and_withholding_come_as_one_pair(solo):  # noqa: F811
    door = _daniel(solo)
    alone = door.call(
        "keep_tax_facts", {"status": "married filing jointly", "spouse_wages": "41200"}
    )
    assert alone["status"] == "needs" and "together" in alone["say"]
    put = door.call(
        "keep_tax_facts",
        {"status": "married filing jointly", "spouse_wages": "$41,200", "spouse_withheld": "3,100"},
    )
    assert put["status"] == "set", put
    assert "your spouse's 2026 wages of $41,200.00, $3,100.00 withheld" in put["say"]
    got = _said(door)
    assert got["filing_status"] == "married filing jointly"
    assert got["spouse"] == {"wages": "41200.00", "withheld": "3100.00"}


def test_a_spouses_wages_never_land_on_a_single_return(solo):  # noqa: F811
    door = _daniel(solo)
    door.call("keep_tax_facts", {"status": "single"})
    put = door.call("keep_tax_facts", {"spouse_wages": "41200", "spouse_withheld": "0"})
    assert put["status"] == "needs" and "joint return" in put["say"]
    assert "spouse" not in _said(door)


def test_last_years_tax_and_each_estimate_paid_are_kept(solo):  # noqa: F811
    door = _daniel(solo)
    assert door.call("keep_tax_facts", {"last_year_tax": "4,210"})["status"] == "set"
    put = door.call("keep_tax_facts", {"estimate_paid": "450", "estimate_day": "2026-04-15"})
    assert "an estimated payment of $450.00 on April 15, 2026, toward 2026" in put["say"]
    ahead = door.call("keep_tax_facts", {"estimate_paid": "450", "estimate_day": "2026-12-15"})
    assert ahead["status"] == "needs"  # not paid yet
    got = _said(door)
    assert got["last_year_tax"] == "4210.00"
    assert got["estimates_paid"] == [{"date": "2026-04-15", "dollars": "450.00"}]


def test_nothing_said_is_asked_for(solo):  # noqa: F811
    assert _daniel(solo).call("keep_tax_facts", {})["status"] == "needs"


# ---- who reads and keeps them (the owner, 2026-11-15, after decision 2) -------------------
# "I'm the president... I'm doing 100% of the work": whoever looks over every
# month keeps a missionary's tax facts and trips and reads their packet, the
# way they keep the missionary's payers. Decision 2's privacy waits for a
# ministry whose missionary has a tax picture of their own.


def test_the_packet_asks_once_about_support_tim_cannot_see(solo):  # noqa: F811
    """Persona night Y07 (2026-11-16): Tim gave Eli a confident zero-tax
    packet, then learned in three more turns that his board and his mom send
    him support straight to him. Until they say whether any support reaches
    them where Tim can't see, the packet's first words ask it, once."""
    daniel = _daniel(solo)
    says = daniel.call("tax_packet", {"year": "2026"})["says"]
    assert "Before any numbers, ask" in says and "keep_tax_facts" in says
    bad = daniel.call("keep_tax_facts", {"year": "2026", "support_elsewhere": "maybe"})
    assert bad["status"] == "needs"
    done = daniel.call("keep_tax_facts", {"year": "2026", "support_elsewhere": "none"})
    assert done["status"] == "set" and "all the support" in done["say"]
    packet = daniel.call("tax_packet", {"year": "2026"})
    assert "Before any numbers" not in packet["says"]
    assert packet["what_they_said"]["support_elsewhere"] == "none"
    some = daniel.call("keep_tax_facts", {"year": "2026", "support_elsewhere": "some"})
    assert "keep_payers" in some["say"]
    assert _said(daniel)["support_elsewhere"] == "some"


def test_whoever_looks_over_every_month_keeps_a_missionarys_tax_facts(solo):  # noqa: F811
    linda = _door(solo, "linda-park")
    names = {s.name for s in linda.specs()}
    assert {"keep_tax_facts", "keep_travel", "tax_packet"} <= names
    put = linda.call("keep_tax_facts", {"person": "Daniel Reyes", "status": "single"})
    assert put["status"] == "set", put
    put = linda.call(
        "keep_travel", {"person": "Daniel Reyes", "left": "2026-01-08", "country": "Nepal"}
    )
    assert put["status"] == "set", put
    got = linda.call("tax_packet", {"year": "2026", "person": "Daniel Reyes"})
    assert got["what_they_said"]["filing_status"] == "single"
    assert got["what_they_said"]["trips"][0]["left"] == "2026-01-08"
    assert _said(_daniel(solo))["filing_status"] == "single"  # the same facts, his own


def test_whoever_looks_over_every_month_says_whose_facts(solo):  # noqa: F811
    linda = _door(solo, "linda-park")
    put = linda.call("keep_tax_facts", {"status": "single"})
    assert put["status"] == "needs" and "missionary" in put["say"]
    put = linda.call("keep_tax_facts", {"person": "Linda Park", "status": "single"})
    assert put["status"] == "needs"  # she files no packet of her own


def test_a_missionary_can_still_share_their_packet(solo):  # noqa: F811
    door = _daniel(solo)
    put = door.call("keep_tax_facts", {"share_with": "Linda Park"})
    assert put["say"] == "Done: Tim has your packet shared with Linda Park."
    door.call("keep_tax_facts", {"share_with": "no one"})
    assert _said(door)["filing_status"] == ""


def test_a_packet_is_shared_only_with_someone_on_the_ministrys_list(solo):  # noqa: F811
    put = _daniel(solo).call("keep_tax_facts", {"share_with": "Pat Preparer"})
    assert put["status"] == "needs" and "isn't on your ministry's list" in put["say"]


# ---- keep_travel -------------------------------------------------------------------------


def test_a_trip_is_kept_and_said_back(solo):  # noqa: F811
    door = _daniel(solo)
    put = door.call("keep_travel", {"left": "2026-01-08", "back": "2026-06-02", "country": "Nepal"})
    put.pop("next")  # the next thing ends every door answer (chunk 1); tested in test_next_thing
    assert put == {
        "status": "set",
        "say": "Done: you left the United States on January 8, 2026 for Nepal and came back "
        "on June 2, 2026.",
    }
    still = door.call("keep_travel", {"left": "2026-07-01", "country": "Nepal"})
    assert still["say"].endswith("and are still there.")
    assert _said(door)["trips"] == [
        {"left": "2026-01-08", "back": "2026-06-02", "country": "Nepal"},
        {"left": "2026-07-01", "back": "", "country": "Nepal"},
    ]


def test_a_trip_that_overlaps_another_is_asked_about(solo):  # noqa: F811
    door = _daniel(solo)
    door.call("keep_travel", {"left": "2026-01-08", "back": "2026-06-02", "country": "Nepal"})
    put = door.call("keep_travel", {"left": "2026-05-20", "back": "2026-07-01", "country": "India"})
    assert put["status"] == "needs" and "overlaps the trip to Nepal" in put["say"]
    # The same trip said again by its day left replaces it.
    again = door.call(
        "keep_travel", {"left": "2026-01-08", "back": "2026-05-01", "country": "Nepal"}
    )
    assert again["status"] == "set"
    assert [t["back"] for t in _said(door)["trips"]] == ["2026-05-01"]


def test_days_that_cannot_be_are_asked_again(solo):  # noqa: F811
    door = _daniel(solo)
    for args in (
        {"left": "January 8", "country": "Nepal"},
        {"left": "2026-06-02", "back": "2026-01-08", "country": "Nepal"},
        {"left": "2026-11-01", "country": "Nepal"},  # after today
        {"left": "2026-01-08"},  # no country
    ):
        assert door.call("keep_travel", args)["status"] == "needs", args
    assert _said(door)["trips"] == []


def test_a_trip_said_wrong_is_forgotten(solo):  # noqa: F811
    door = _daniel(solo)
    door.call("keep_travel", {"left": "2026-02-01", "back": "2026-02-10", "country": "Kenya"})
    put = door.call("keep_travel", {"left": "2026-02-01", "forget": True})
    assert put["say"] == "Done: Tim forgot the trip that left on February 1, 2026."
    assert _said(door)["trips"] == []
