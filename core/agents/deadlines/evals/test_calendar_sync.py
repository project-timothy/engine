"""deadlines/calendar: deadlines written straight into the person's own calendar.

The calendar file carries dates but not reminders in Outlook or Google, so the
engine writes the events itself (the Ask Tim design note, 2026-10-04). The
promises under test:

1. Which calendar is decided in code, in a fixed order: the tenant's setting,
   the organization's Microsoft mail connection, the address's domain, its
   public mail records; the calendar file is the fallback.
2. Each open deadline is one all-day event carrying the final-week reminder;
   a deadline marked done or moved has its old event removed. A re-run with
   nothing changed writes nothing.
3. Writing to someone's calendar is an external act: an approval card for the
   plan, unless the tenant's own policy names the calendar unattended. A
   missing calendar permission is named, never a silent failure.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.adapters.graph_mail import GraphAuthError
from core.agents.deadlines import calendar_sync as cs
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

OBLIGATIONS = """
[[obligation]]
id = "permit"
title = "Work permit renewal"
who = "Jane"
due = 2026-11-20

[[obligation]]
id = "report"
title = "Quarterly report"
due = 2026-12-31
every = "3m"
lead_days = [14, 3]
"""


class FakeCalendar:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._n = 0

    def create_all_day(self, *, subject, day, body, reminder_minutes, transaction_id, time_zone):
        self._n += 1
        eid = f"E{self._n}"
        self.events[eid] = {"subject": subject, "day": day, "reminder": reminder_minutes}
        self.calls.append(("create", subject, day.isoformat(), reminder_minutes, transaction_id))
        return eid

    def update_all_day(self, event_id, *, subject, day, body, reminder_minutes, time_zone):
        self.events[event_id] = {"subject": subject, "day": day, "reminder": reminder_minutes}
        self.calls.append(("update", event_id, subject))

    def delete(self, event_id):
        self.events.pop(event_id, None)
        self.calls.append(("delete", event_id))


@pytest.fixture
def world(tmp_path, monkeypatch):
    ob = tmp_path / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    cal = FakeCalendar()
    monkeypatch.setattr(cs, "_calendar_client", lambda ctx: cal)
    return {"tmp": tmp_path, "ob": ob, "cal": cal, "ledger_dir": tmp_path / "data"}


def _sync(world, today="2026-10-04", **params):
    base = {"obligations_file": str(world["ob"]), "today": today, "calendar": "graph"}
    return run(
        "demo", "deadlines", "calendar", params={**base, **params}, ledger_dir=world["ledger_dir"]
    )


def _done(world, ob_id, today):
    run(
        "demo",
        "deadlines",
        "done",
        params={"obligations_file": str(world["ob"]), "id": ob_id, "today": today},
        ledger_dir=world["ledger_dir"],
    )


def _cards(world):
    root = resolve_ledger_root("demo", world["ledger_dir"])
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id, status FROM approval_queue WHERE action_type = ? ORDER BY id",
            (cs.CAL_ACTION,),
        ).fetchall()
    return [dict(r) for r in rows]


def _decide(world, card_id, status):
    root = resolve_ledger_root("demo", world["ledger_dir"])
    with Ledger.open(root) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = ? WHERE id = ?", (status, card_id))
        ledger.conn.commit()


# ---- 1. which calendar ----------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "client_id", "address", "mx", "want"),
    [
        ("google", "app-1", "a@corp.example", [], "google"),
        ("", "app-1", "a@gmail.com", [], "graph"),
        ("", "", "a@gmail.com", [], "google"),
        ("", "", "a@outlook.com", [], "graph"),
        ("", "", "a@icloud.com", [], "ics"),
        ("", "", "a@corp.example", ["corp-example.mail.protection.outlook.com"], "graph"),
        ("", "", "a@corp.example", ["aspmx.l.google.com"], "google"),
        ("", "", "a@corp.example", ["mx.other.example"], "ics"),
        ("", "", "", [], "ics"),
    ],
)
def test_the_calendar_is_decided_in_a_fixed_order(setting, client_id, address, mx, want):
    provider, reason = cs.detect_provider(
        setting=setting, mail_client_id=client_id, address=address, mx_lookup=lambda d: mx
    )
    assert provider == want and reason


def test_mx_answers_parse():
    answer = {"Answer": [{"type": 15, "data": "0 corp-example.mail.protection.outlook.com."}]}
    assert cs.mx_hosts_from_doh(answer) == ["corp-example.mail.protection.outlook.com"]


@pytest.mark.parametrize(("leads", "minutes"), [([90, 30, 7], 10080), ([14, 3], 4320), ([0], 0)])
def test_the_calendar_carries_the_final_week_reminder(leads, minutes):
    assert cs.reminder_minutes(leads) == minutes


# ---- 2. one event per open deadline ---------------------------------------------


def test_unattended_sync_writes_one_event_each_then_nothing(world):
    out = _sync(world, unattended="calendar")
    assert out.status == "ok"
    creates = [c for c in world["cal"].calls if c[0] == "create"]
    assert [(c[1], c[2], c[3]) for c in creates] == [
        ("Work permit renewal (Jane)", "2026-11-20", 10080),
        ("Quarterly report", "2026-12-31", 4320),
    ]
    assert creates[0][4] != creates[1][4]  # idempotent create keys differ
    world["cal"].calls.clear()
    _sync(world, "2026-10-05", unattended="calendar")
    assert world["cal"].calls == []


def test_done_removes_the_event_and_a_recurring_one_rolls(world):
    _sync(world, unattended="calendar")
    _done(world, "report", "2026-10-04")
    world["cal"].calls.clear()
    _sync(world, "2026-10-05", unattended="calendar")
    ops = [c[0] for c in world["cal"].calls]
    assert ops.count("delete") == 1 and ops.count("create") == 1
    created = [c for c in world["cal"].calls if c[0] == "create"][0]
    assert created[2] == "2027-03-31"


def test_a_moved_date_replaces_its_event(world):
    _sync(world, unattended="calendar")
    world["ob"].write_text(OBLIGATIONS.replace("2026-11-20", "2026-11-27"))
    world["cal"].calls.clear()
    _sync(world, "2026-10-05", unattended="calendar")
    assert [c[0] for c in world["cal"].calls] == ["delete", "create"]
    assert world["cal"].calls[1][2] == "2026-11-27"


# ---- 3. approval, providers, permission -------------------------------------------


def test_without_the_grant_the_plan_waits_for_a_card(world):
    out = _sync(world)
    assert out.status == "needs_approval"
    (card,) = _cards(world)
    assert world["cal"].calls == []
    _sync(world, "2026-10-05")  # still pending: nothing written, no second card
    assert world["cal"].calls == [] and len(_cards(world)) == 1
    _decide(world, card["id"], "approved")
    _sync(world, "2026-10-06")
    assert len([c for c in world["cal"].calls if c[0] == "create"]) == 2


def test_a_rejected_plan_is_not_asked_again_until_it_changes(world):
    _sync(world)
    (card,) = _cards(world)
    _decide(world, card["id"], "rejected")
    _sync(world, "2026-10-05")
    assert len(_cards(world)) == 1 and world["cal"].calls == []


@pytest.mark.parametrize("provider", ["ics", "google", "off"])
def test_other_calendars_write_nothing_yet(world, provider):
    out = _sync(world, calendar=provider, unattended="calendar")
    assert out.status == "ok" and world["cal"].calls == []
    assert provider in out.summary


def test_a_missing_calendar_permission_is_named(world, monkeypatch):
    def denied(ctx):
        raise GraphAuthError("silent token acquisition failed: consent required")

    monkeypatch.setattr(cs, "_calendar_client", denied)
    out = _sync(world, unattended="calendar")
    assert out.status == "ok"
    (anomaly,) = [a for a in out.anomalies if a.code == "deadlines.calendar_permission"]
    assert "Calendars.ReadWrite" in anomaly.detail and "engine mail consent" in anomaly.detail


def test_shadow_writes_nothing(world):
    out = run(
        "demo",
        "deadlines",
        "calendar",
        params={
            "obligations_file": str(world["ob"]),
            "today": "2026-10-04",
            "calendar": "graph",
            "unattended": "calendar",
        },
        ledger_dir=world["ledger_dir"],
        shadow=True,
    )
    assert out.status == "ok" and world["cal"].calls == []
    assert "would create 2" in out.summary


def test_dates_are_dates():
    assert cs.next_day(date(2026, 12, 31)) == date(2027, 1, 1)
