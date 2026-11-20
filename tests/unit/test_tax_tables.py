"""The yearly tax tables (board chunk c36, PR 1): one file a year under
``core/tax/tables/``, every figure with its value, the document it came
from, where in that document, and the day someone read it there. A figure
without all four fails the build; a year without a file is refused in
plain words, never guessed."""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal

import pytest

from core.tax import figures


def test_every_figure_carries_its_value_source_address_and_confirmed_day():
    for path in sorted(figures.TABLES.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert figures.problems(data) == [], path.name


def test_the_guard_names_a_figure_with_no_source_or_no_confirmed_day():
    data = {
        "year": "2026",
        "sources": {"rp": {"title": "A revenue procedure", "url": "https://www.irs.gov/x.pdf"}},
        "figures": {
            "a": {"value": "1", "source": "rp", "address": "section 1", "confirmed": ""},
            "b": {"value": "1", "source": "", "address": "section 2", "confirmed": "2026-11-15"},
            "c": {"value": "1", "source": "nowhere", "address": "s", "confirmed": "2026-11-15"},
            "d": {"value": "1", "source": "rp", "address": "", "confirmed": "2026-11-15"},
            "e": {"value": "1", "source": "rp", "address": "s", "confirmed": "someday"},
            "f": {"value": "1", "source": "rp", "address": "s", "confirmed": "2999-01-01"},
            "g": {"source": "rp", "address": "s", "confirmed": "2026-11-15"},
            "h": {"value": "1", "source": "rp", "address": "s", "confirmed": "2026-11-15"},
        },
    }
    named = {p.split(":")[0] for p in figures.problems(data, today=date(2026, 11, 15))}
    assert named == {"a", "b", "c", "d", "e", "f", "g"}


def test_a_source_needs_a_title_and_a_government_address():
    data = {
        "year": "2026",
        "sources": {"blog": {"title": "Someone's page", "url": "https://example.com/taxes"}},
        "figures": {},
    }
    assert figures.problems(data) == ["blog: its address is not an IRS or SSA page"]


def test_a_year_with_no_file_is_refused_in_plain_words():
    with pytest.raises(figures.NoYear) as no:
        figures.year("2019")
    assert str(no.value) == "Tim doesn't hold 2019's tax figures yet."
    assert isinstance(no.value, KeyError)  # what packet.py already catches


def test_2026_figures_read_as_cents_and_rates():
    y = figures.year("2026")
    assert y.cents("standard_deduction.single") == 1610000
    assert y.cents("standard_deduction.married_jointly") == 3220000
    assert y.cents("self_employment.wage_base") == 18450000
    assert y.cents("exclusion.most") == 13290000
    assert y.rate("self_employment.earnings_share") == Decimal("0.9235")
    assert y.rate("qualified_business_income.share") == Decimal("0.20")


def test_each_bracket_table_agrees_with_its_own_printed_base_amounts():
    """The table prints "$5,800 plus 22% of the excess": each base must be
    the tax on everything below it, so a mistyped row fails here."""
    y = figures.year("2026")
    for status in figures.STATUSES:
        rows = y.brackets(status)
        assert rows[-1].up_to is None
        assert rows[0].base == 0
        for below, row in zip(rows, rows[1:], strict=False):
            assert below.up_to is not None
            figured = below.base + (Decimal(below.up_to - below.floor) * below.rate)
            assert row.base == figured, (status, row)
            assert row.floor == below.up_to


def test_2026_brackets_as_revenue_procedure_2025_32_prints_them():
    single = figures.year("2026").brackets("single")
    assert [(r.up_to, r.rate) for r in single] == [
        (1240000, Decimal("0.10")),
        (5040000, Decimal("0.12")),
        (10570000, Decimal("0.22")),
        (20177500, Decimal("0.24")),
        (25622500, Decimal("0.32")),
        (64060000, Decimal("0.35")),
        (None, Decimal("0.37")),
    ]
    assert single[-1].base == Decimal("19297925")


def test_a_figure_the_year_does_not_hold_is_named():
    with pytest.raises(figures.NoFigure) as no:
        figures.year("2026").cents("standard_deduction.widowed")
    assert "standard_deduction.widowed" in str(no.value)


def test_the_figures_name_where_they_came_from():
    cited = figures.year("2026").cited("self_employment.wage_base", "exclusion.most")
    assert "Revenue Procedure 2025-32" in cited
    assert "Schedule SE" in cited
