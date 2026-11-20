"""Days abroad from the travel dates the person said (board chunk c38, PR 3;
the plan of 2026-11-15, gap E).

The physical presence test for the Foreign Earned Income Exclusion: 330
full days outside the United States in any 12 months in a row. A full day
is a whole day abroad, so the day they left and the day they came back are
not full days. A 12-month period starts on any day and ends the day before
the same day a year later: 365 days, or 366 when it spans February 29. A
trip still open runs to the end of the period asked about. Tim finds the
period with the most full days that reaches into the tax year."""

from __future__ import annotations

from datetime import date

from core.tax import presence

T = presence.Stay


def test_a_full_year_abroad_counts_365():
    got = presence.best(  # left the day before the year and came back the day after
        [T("2025-12-31", "2027-01-01")], "2026"
    )
    assert got.days == 365 and got.qualifies
    assert (got.start, got.end) == (date(2026, 1, 1), date(2026, 12, 31))


def test_a_36_day_trip_home_inside_the_window_leaves_329_and_fails():
    stays = [T("2025-12-01", "2026-06-01"), T("2026-07-06", "2027-03-01")]
    got = presence.best(stays, "2026")
    assert got.days == 329 and not got.qualifies  # June 1 to July 6 at home: 36 days


def test_the_day_they_left_and_came_back_are_not_full_days():
    assert presence.full_days([T("2026-03-01", "2026-03-05")]) == {
        date(2026, 3, 2),
        date(2026, 3, 3),
        date(2026, 3, 4),
    }
    assert presence.full_days([T("2026-03-01", "2026-03-02")]) == set()  # a night away


def test_a_trip_still_open_runs_to_the_end_of_the_period():
    got = presence.best([T("2026-01-06", "")], "2026")
    assert got.days == 365 and got.qualifies
    assert (got.start, got.end) == (date(2026, 1, 7), date(2027, 1, 6))


def test_a_period_that_spans_february_29_has_366_days():
    got = presence.best([T("2027-02-28", "2028-03-02")], "2027")
    assert got.days == 366 and (got.end - got.start).days == 365


def test_of_two_periods_as_good_the_one_most_in_the_year_wins():
    # Abroad from mid-2025 on: every period counts all its days; the one
    # that is the calendar year itself is the most use for that year.
    got = presence.best([T("2025-06-01", "")], "2026")
    assert (got.start, got.end) == (date(2026, 1, 1), date(2026, 12, 31))


def test_no_trips_count_nothing():
    got = presence.best([], "2026")
    assert got.days == 0 and not got.qualifies and got.start is None


def test_a_period_that_runs_past_today_is_named():
    got = presence.best([T("2026-01-06", "")], "2026", today=date(2026, 11, 15))
    assert got.days == 365 and got.qualifies and got.ahead == 52  # Nov 16 to Jan 6, if they stay
    done = presence.best([T("2026-01-06", "")], "2026", today=date(2027, 2, 1))
    assert done.ahead == 0


# ---- on the packet: the travel dates first, the bank second -----------------------------

from core.agents.expenses import packet  # noqa: E402
from tests.unit.test_tax_packet import GOT, ROWS  # noqa: E402

LEFT = [{"left": "2026-01-06", "back": "", "country": "Nepal"}]


def test_the_packet_counts_from_the_travel_dates_and_names_a_bank_day_at_home():
    got = packet.from_travel(ROWS, "2026", LEFT, today=date(2026, 11, 15))
    assert got["full_days"] == 365 and got["qualifies"]
    assert (got["from"], got["to"]) == ("2026-01-07", "2027-01-06")
    assert got["days_still_ahead"] == 52
    # The bank shows rupees from an ATM on January 5, the day before they said they left.
    assert got["bank_abroad_on_days_home"] == ["2026-01-05"]
    assert got["days_with_spending_abroad"] == 3  # the bank's draft stays beside it
    assert "365 full days abroad" in got["says"] and "only if they stay" in got["says"]
    assert "1 day they said they were in the United States (Jan 5)" in got["says"]


def test_a_trip_said_whole_leaves_no_bank_day_unexplained():
    trips = [{"left": "2026-01-04", "back": "2026-04-01", "country": "Nepal"}]
    got = packet.from_travel(ROWS, "2026", trips, today=date(2026, 11, 15))
    assert got["bank_abroad_on_days_home"] == []
    assert got["full_days"] == 86 and not got["qualifies"]  # Jan 5 to Mar 31
    assert "244 short of the 330" in got["says"]


def test_with_no_travel_dates_the_bank_draft_stands():
    assert packet.from_travel(ROWS, "2026", [], None) == packet.days_abroad(ROWS, "2026")


def test_the_page_shows_the_count_from_the_travel_dates():
    days = packet.from_travel(ROWS, "2026", LEFT, today=date(2026, 11, 15))
    blocks = packet.page({**GOT, "days_abroad": days})
    t = {b[1]: b[2] for b in blocks if b[0] == "table"}
    rows = t["Days abroad, for the Foreign Earned Income Exclusion"]
    assert rows[0] == (
        "Full days abroad from January 7, 2026 to January 6, 2027, from their travel dates",
        "365",
        True,
    )
    assert (
        "Spending abroad on January 5, 2026, a day they said they were home",
        "ask",
        False,
    ) in rows
    words = " ".join(b[1] for b in blocks if b[0] == "text")
    assert "the day they left and the day they came back are not counted" in words
    assert "52 of the days are still ahead" in words


# ---- through the door ----------------------------------------------------------------------

from tests.unit.test_door_my_words import solo  # noqa: E402,F401  (fixture)
from tests.unit.test_payers import _paid  # noqa: E402


def test_the_packet_reads_the_trips_they_told_tim(solo):  # noqa: F811
    door = _paid(solo)
    before = door.call("tax_packet", {"year": "2026"})["days_abroad"]
    assert "full_days" not in before  # no travel dates: the bank's draft
    door.call("keep_travel", {"left": "2025-06-01", "country": "Nepal"})
    got = door.call("tax_packet", {"year": "2026"})
    days = got["days_abroad"]
    assert days["full_days"] == 365 and days["qualifies"]
    assert (days["from"], days["to"]) == ("2026-01-01", "2026-12-31")
    assert "365 full days abroad" in got["says"]


# ---- the days the exclusion reads (c39 PR 3) ------------------------------------------------


def test_the_scholarship_year_qualifies_359_days_of_2026():
    from core.tax import presence

    stays = [presence.Stay("2026-01-06", "2027-01-07")]
    p = presence.best(stays, "2026")
    assert presence.in_year(stays, p, "2026") == (359, 359)


def test_a_trip_home_inside_the_period_is_a_qualifying_day_but_not_a_day_abroad():
    from core.tax import presence

    stays = [presence.Stay("2025-06-30", "2026-03-01"), presence.Stay("2026-03-10", "2027-01-02")]
    p = presence.best(stays, "2026")
    qualifying, abroad = presence.in_year(stays, p, "2026")
    assert qualifying == 365
    assert abroad == 365 - 10  # March 1 through 10: back, home, and left again


def test_no_period_no_days():
    from core.tax import presence

    assert presence.in_year([], presence.Presence(0, None, None), "2026") == (0, 0)
