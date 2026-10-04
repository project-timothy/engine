"""The Graph calendar client: the exact requests it makes, with a fake transport."""

from __future__ import annotations

from datetime import date

import pytest

from core.adapters.graph_calendar import GraphCalendarClient, GraphCalendarError


class Recorder:
    def __init__(self, reply=None, fail: int | None = None):
        self.calls: list[tuple] = []
        self.reply = reply
        self.fail = fail

    def __call__(self, method, url, *, body, headers):
        self.calls.append((method, url, body, headers))
        if self.fail:
            raise GraphCalendarError(f"HTTP {self.fail}", status=self.fail)
        return self.reply


def _client(transport):
    return GraphCalendarClient(token_provider=lambda: "TOKEN", transport=transport)


def test_create_is_an_all_day_event_with_one_reminder_and_a_transaction_id():
    t = Recorder(reply={"id": "AAMk1"})
    eid = _client(t).create_all_day(
        subject="1099-NEC",
        day=date(2027, 1, 31),
        body="Kind: tax",
        reminder_minutes=10080,
        transaction_id="tx-1",
        time_zone="America/New_York",
    )
    assert eid == "AAMk1"
    method, url, body, headers = t.calls[0]
    assert method == "POST" and url.endswith("/me/events")
    assert headers["Authorization"] == "Bearer TOKEN"
    assert body["isAllDay"] is True and body["transactionId"] == "tx-1"
    assert body["start"] == {"dateTime": "2027-01-31T00:00:00", "timeZone": "America/New_York"}
    assert body["end"]["dateTime"] == "2027-02-01T00:00:00"
    assert body["reminderMinutesBeforeStart"] == 10080 and body["isReminderOn"] is True
    assert body["showAs"] == "free"


def test_update_patches_the_event():
    t = Recorder()
    _client(t).update_all_day(
        "AAMk1",
        subject="x",
        day=date(2027, 1, 31),
        body="",
        reminder_minutes=0,
        time_zone="UTC",
    )
    assert t.calls[0][0] == "PATCH" and t.calls[0][1].endswith("/me/events/AAMk1")


def test_deleting_an_event_already_gone_is_done():
    _client(Recorder(fail=404)).delete("gone")
    with pytest.raises(GraphCalendarError):
        _client(Recorder(fail=500)).delete("x")
