"""Tenant-local calendar helpers: the UTC-slice diseases, pinned."""

from __future__ import annotations

from datetime import UTC, datetime

from core.engine.clock import local_date, local_month, local_today

TZ = "America/New_York"


def test_evening_utc_timestamp_is_the_previous_local_date():
    # 02:30 UTC on the 13th is 22:30 ET on the 12th.
    assert local_date("2026-08-13T02:30:00+00:00", TZ) == "2026-08-12"


def test_month_boundary_does_not_flip_hours_early():
    # 8/31 9 PM ET is already 9/1 in UTC; the tenant month is still August.
    at = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    assert local_month(TZ, at=at) == "2026-08"
    assert local_today(TZ, at=at) == "2026-08-31"


def test_bare_date_passes_through():
    assert local_date("2026-08-12", TZ) == "2026-08-12"


def test_offsetless_timestamp_reads_as_utc():
    assert local_date("2026-08-13T02:30:00", TZ) == "2026-08-12"


def test_daytime_timestamp_is_the_same_date_in_both():
    assert local_date("2026-08-13T15:00:00+00:00", TZ) == "2026-08-13"
