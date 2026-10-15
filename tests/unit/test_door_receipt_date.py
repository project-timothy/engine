"""A receipt is dated on the person's own clock (found on the practice box,
2026-10-12): Carol's 9:30 PM Eastern receipt was named with the next day's
UTC date, so one snapped on the last evening of a month would have filed
into the next month. The day is the person's own (the zone onboarding
writes beside them), else the ministry's."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.tools.receipts import Claim, ReceiptDesk, take_photo
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipts import JPEG


def _snap(solo, at: datetime) -> str:  # noqa: F811
    desk = ReceiptDesk()
    token = desk.issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    status, said = take_photo(
        desk,
        token,
        JPEG,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        now=at,
    )
    assert status == 200, said
    names = [p.name for p in Path.cwd().rglob("*_receipt_*") if "/drop/" in str(p)]
    assert len(names) == 1, names
    return names[0][:10]


def _zone(solo, zone: str) -> None:  # noqa: F811
    authority = Path(solo["root"]) / "living-water" / "authority.toml"
    text = authority.read_text()
    block = re.search(r"(?ms)^\[people\.daniel-reyes\].*?(?=^\[|\Z)", text).group(0)
    moved = re.sub(r'(?m)^zone = ".*"$', f'zone = "{zone}"', block)
    authority.write_text(text.replace(block, moved))


def test_an_evening_receipt_keeps_its_own_day(solo):  # noqa: F811
    # 01:30 UTC on the 13th is 9:30 PM on the 12th in New York.
    assert _snap(solo, datetime(2026, 10, 13, 1, 30, tzinfo=UTC)) == "2026-10-12"


def test_the_last_evening_of_a_month_stays_in_that_month(solo):  # noqa: F811
    assert _snap(solo, datetime(2026, 11, 1, 2, 0, tzinfo=UTC)) == "2026-10-31"


def test_a_missionary_abroad_dates_it_on_her_own_clock(solo):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    # 20:00 UTC on Oct 31 is 1:45 AM on Nov 1 in Kathmandu.
    assert _snap(solo, datetime(2026, 10, 31, 20, 0, tzinfo=UTC)) == "2026-11-01"


def test_a_person_with_no_zone_of_their_own_uses_the_ministrys(solo):  # noqa: F811
    authority = Path(solo["root"]) / "living-water" / "authority.toml"
    authority.write_text(re.sub(r'(?m)^zone = ".*"\n', "", authority.read_text()))
    assert _snap(solo, datetime(2026, 10, 13, 1, 30, tzinfo=UTC)) == "2026-10-12"


def test_a_zone_this_host_does_not_know_uses_the_ministrys(solo):  # noqa: F811
    _zone(solo, "Mars/Olympus")
    assert _snap(solo, datetime(2026, 10, 13, 1, 30, tzinfo=UTC)) == "2026-10-12"
