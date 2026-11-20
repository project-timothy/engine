"""The span of the books a person asks about (t-books): their words, read to a
first and last day."""

from __future__ import annotations

from datetime import date

import pytest

from core.agents.expenses.periods import NotAPeriod, read

TODAY = date(2026, 10, 29)


@pytest.mark.parametrize(
    ("said", "start", "end", "words"),
    [
        ("", "2026-01-01", "2026-10-29", "2026 through October 29"),
        ("YTD", "2026-01-01", "2026-10-29", "2026 through October 29"),
        ("year to date", "2026-01-01", "2026-10-29", "2026 through October 29"),
        ("2025-ytd", "2025-01-01", "2025-12-31", "2025"),
        ("2026-09", "2026-09-01", "2026-09-30", "September 2026"),
        ("September", "2026-09-01", "2026-09-30", "September 2026"),
        ("sept", "2026-09-01", "2026-09-30", "September 2026"),
        ("December", "2025-12-01", "2025-12-31", "December 2025"),
        ("feb 2024", "2024-02-01", "2024-02-29", "February 2024"),
        ("last month", "2026-09-01", "2026-09-30", "September 2026"),
        ("this month", "2026-10-01", "2026-10-31", "October 2026"),
        ("2026-Q3", "2026-07-01", "2026-09-30", "Q3 2026"),
        ("Q1", "2026-01-01", "2026-03-31", "Q1 2026"),
        ("q4 2025", "2025-10-01", "2025-12-31", "Q4 2025"),
        ("last quarter", "2026-07-01", "2026-09-30", "Q3 2026"),
        ("this quarter", "2026-10-01", "2026-12-31", "Q4 2026"),
        ("2025", "2025-01-01", "2025-12-31", "2025"),
        ("last year", "2025-01-01", "2025-12-31", "2025"),
    ],
)
def test_a_persons_words_read_as_a_span(said, start, end, words):
    p = read(said, TODAY)
    assert (p.start.isoformat(), p.end.isoformat(), p.words) == (start, end, words)


def test_last_quarter_in_january_is_last_years_fourth():
    assert read("last quarter", date(2027, 1, 5)).words == "Q4 2026"


def test_the_months_stop_at_today():
    assert read("2026", TODAY).months(TODAY) == [f"2026-{m:02d}" for m in range(1, 11)]
    assert read("Q3", TODAY).months(TODAY) == ["2026-07", "2026-08", "2026-09"]


@pytest.mark.parametrize("said", ["2026-13", "ma", "next week", "q5"])
def test_words_that_are_no_span_are_refused(said):
    with pytest.raises(NotAPeriod):
        read(said, TODAY)
