"""Mileage at two rates (board chunk c34, PR 6). Every ministry drive is
worth the IRS standard rate on its own day, read from the year's tax table;
the ministry pays it back at its own rate, and never above the IRS rate
(anything above counts as pay). When the ministry's rate is lower, the
difference is a cost on the person's Schedule C."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.agents.expenses import packet
from core.engine.config import InstitutionSettings
from core.institution import InstitutionError, irs_mileage_rate, mileage_rate
from core.tax import mileage
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_mileage_purpose import _drive, _drives, _say
from tests.unit.test_ministry_lines import _door

GRACE = InstitutionSettings(name="Grace Fellowship", mileage_rate=Decimal("0.70"))


def test_the_irs_rate_comes_from_the_years_table_on_the_drives_own_day():
    assert mileage.irs_rate(date(2026, 3, 1)) == Decimal("0.725")
    assert mileage.irs_rate(date(2026, 6, 30)) == Decimal("0.725")
    assert mileage.irs_rate(date(2026, 7, 1)) == Decimal("0.76")


def test_a_year_tim_holds_no_figures_for_is_refused_in_plain_words():
    with pytest.raises(InstitutionError, match="Tim doesn't hold the IRS mileage rate for 2025"):
        irs_mileage_rate(date(2025, 12, 1))


def test_the_ministry_rate_pays_back_when_it_is_at_or_below_the_irs_rate():
    assert mileage_rate(GRACE, date(2026, 3, 1)) == (Decimal("0.70"), "Grace Fellowship")
    at_irs = InstitutionSettings(name="Grace Fellowship", mileage_rate=Decimal("0.725"))
    assert mileage_rate(at_irs, date(2026, 3, 1))[0] == Decimal("0.725")


def test_a_ministry_rate_above_the_irs_rate_is_refused_on_that_day():
    high = InstitutionSettings(name="Grace Fellowship", mileage_rate=Decimal("0.74"))
    with pytest.raises(InstitutionError) as said:
        mileage_rate(high, date(2026, 3, 1))
    words = str(said.value)
    assert "Grace Fellowship pays drives back at 74 cents a mile" in words
    assert "above the IRS rate of 72.5 cents on Mar 1, 2026" in words
    assert "counts as pay" in words
    # From July the IRS rate is 76 cents, so the same rate is in bounds.
    assert mileage_rate(high, date(2026, 8, 3))[0] == Decimal("0.74")


def test_what_drives_paid_below_the_irs_rate_left_unpaid():
    drives = [
        (Decimal("14.0"), Decimal("0.725"), Decimal("0.70")),  # 10.15 less 9.80
        (Decimal("100"), Decimal("0.76"), Decimal("0.70")),  # 76.00 less 70.00
        (Decimal("10"), Decimal("0.725"), Decimal("0.725")),  # paid in full
    ]
    assert mileage.below_irs(drives) == 35 + 600


def test_the_drive_line_shows_both_rates_when_the_ministry_pays_less(solo):  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    toml.write_text(
        toml.read_text() + '\n[institution]\nname = "Grace Fellowship"\nmileage_rate = "0.70"\n'
    )
    _drive(solo)
    _say(solo, "ministry: hospital visit")
    [drive] = _drives(solo)
    assert drive["dollars"] == "9.80" and drive["rate_from"] == "Grace Fellowship"
    # Oct 12 falls after July 1: the IRS rate is 76 cents (IR-2026-29).
    assert drive["irs_rate"] == "0.76" and drive["at_irs_rate"] == "10.64"


def test_a_drive_at_a_rate_above_the_irs_rate_waits_and_says_why(solo):  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    toml.write_text(
        toml.read_text() + '\n[institution]\nname = "Grace Fellowship"\nmileage_rate = "0.90"\n'
    )
    _drive(solo)
    _say(solo, "ministry: hospital visit")
    [drive] = _drives(solo)
    assert drive["dollars"] is None
    assert "above the IRS rate" in drive["waiting_on"]


def test_the_difference_is_a_cost_on_schedule_c():
    says: list[str] = []
    pay = [{"form": "1099-NEC", "came_in_seen": "1200.00", "form_says": "1200.00"}]
    drives = [(Decimal("100"), Decimal("0.76"), Decimal("0.70"))]
    got = packet.taxes("2026", pay, [], says, {}, date(2027, 1, 20), drives=drives)
    c = got["schedule_c"]
    assert c["mileage_below_irs_rate"] == "6.00"
    assert c["costs"] == "6.00" and c["net"] == "1194.00"
    assert any("below the IRS mileage rate" in s for s in says)


def test_with_no_ministry_rate_the_packet_is_unchanged():
    says: list[str] = []
    pay = [{"form": "1099-NEC", "came_in_seen": "1200.00", "form_says": "1200.00"}]
    drives = [(Decimal("100"), Decimal("0.76"), Decimal("0.76"))]
    got = packet.taxes("2026", pay, [], says, {}, date(2027, 1, 20), drives=drives)
    assert "mileage_below_irs_rate" not in got["schedule_c"]
    assert got["schedule_c"]["costs"] == "0.00"


def test_the_tax_packet_carries_their_drives_paid_below_the_irs_rate(solo):  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    toml.write_text(
        toml.read_text() + '\n[institution]\nname = "Grace Fellowship"\nmileage_rate = "0.70"\n'
    )
    _drive(solo)
    _say(solo, "ministry: hospital visit")
    got = _door(solo, "daniel-reyes").call("tax_packet", {"year": "2026"})
    assert got["schedule_c"]["mileage_below_irs_rate"] == "0.84"  # 10.64 less 9.80


def test_every_person_facing_change_has_its_faq_entry():
    import tomllib
    from pathlib import Path

    faq = tomllib.loads(Path("core/capabilities/faq.toml").read_text(encoding="utf-8"))
    ids = {e["id"] for e in faq["entry"]}
    assert "mileage-two-rates" in ids
