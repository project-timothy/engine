"""The emails the box sends (#453's delivery half; 2026-10-13): a reviewer
hears once that a person's month is ready, and anyone a card waits on hears
at most once a day that a yes is waiting. The routing lane decides who; the
doorkeeper words and sends them (doorkeeper send-notices), so a notice here
carries people, a kind and a month, never a figure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents.expenses.records import REVIEW_CARD as MONTH_CARD
from core.agents.routing.jobs import NOTICE_EVENT
from core.engine.cli import main
from core.engine.runner import run
from core.ledger import Ledger
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)


def _notices(solo, today: str = "2026-10-14"):  # noqa: F811
    return run(
        "living-water",
        "routing",
        "notices",
        params={"today": today},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _sent(solo) -> list[dict]:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        return [e["payload"] for e in ledger.read_event_log() if e["event_type"] == NOTICE_EVENT]


def test_the_reviewer_hears_each_month_once_and_the_approver_hears_a_yes_waits(solo):  # noqa: F811
    assert _notices(solo).status == "ok"
    got = sorted((n["to"], n["kind"], n["about"], n["month"]) for n in _sent(solo))
    assert got == [
        ("daniel-reyes", "waiting", "", ""),
        ("linda-park", "month_ready", "daniel-reyes", "2026-08"),
        ("linda-park", "month_ready", "daniel-reyes", "2026-09"),
    ]


def test_at_most_once_a_day(solo):  # noqa: F811
    _notices(solo)
    _notices(solo)
    assert len(_sent(solo)) == 3
    _notices(solo, today="2026-10-15")  # the next day: the yes still waits
    waiting = [n for n in _sent(solo) if n["kind"] == "waiting"]
    assert [n["day"] for n in waiting] == ["2026-10-14", "2026-10-15"]
    assert len([n for n in _sent(solo) if n["kind"] == "month_ready"]) == 2


def test_nothing_waiting_means_no_notice(solo):  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        for row in ledger.list_approvals("living-water", status="pending"):
            ledger.resolve_approval("living-water", int(row["id"]), "rejected")
    _notices(solo)
    assert _sent(solo) == []


def test_a_notice_carries_no_figure(solo):  # noqa: F811
    _notices(solo)
    for n in _sent(solo):
        extra = {"people", "more"} if n["kind"] == "waiting" else set()
        assert set(n) == {"id", "to", "kind", "about", "month", "day"} | extra


def test_the_command_hands_the_doorkeeper_one_line_each(solo, capsys):  # noqa: F811
    _notices(solo)
    assert main(["notices", "living-water", "--days", "3650"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 3
    for n in lines:
        extra = {"people", "more"} if n["kind"] == "waiting" else set()
        assert set(n) == {"id", "to", "kind", "about", "month"} | extra


# ---- the Friday check-in on drives (w-mileage-money call 2, the owner's lean
# 2026-10-13): at most one email a week, naming no places, asking only that
# the person tell Tim which drives were for the ministry.

FRIDAY = "2026-10-16"


def _drive_notices(solo) -> list[dict]:  # noqa: F811
    return [n for n in _sent(solo) if n["kind"] == "drives_week"]


def test_on_friday_a_drive_nobody_spoke_for_brings_one_check_in(solo):  # noqa: F811
    from tests.unit.test_mileage_purpose import _drive

    _drive(solo, start="2026-10-12T03:00:00+00:00")
    _drive(solo, start="2026-10-13T03:00:00+00:00")
    _notices(solo, today=FRIDAY)
    _notices(solo, today=FRIDAY)
    assert [(n["to"], n["about"], n["month"]) for n in _drive_notices(solo)] == [
        ("daniel-reyes", "", "")
    ]


def test_no_check_in_midweek(solo):  # noqa: F811
    from tests.unit.test_mileage_purpose import _drive

    _drive(solo, start="2026-10-12T03:00:00+00:00")
    _notices(solo, today="2026-10-14")
    assert _drive_notices(solo) == []


def test_no_check_in_once_every_drive_is_spoken_for(solo):  # noqa: F811
    from tests.unit.test_mileage_purpose import _drive, _say

    _drive(solo, start="2026-10-12T03:00:00+00:00")
    _say(solo, "personal")
    _notices(solo, today=FRIDAY)
    assert _drive_notices(solo) == []


def test_a_drive_from_before_this_week_is_not_asked_about_again(solo):  # noqa: F811
    from tests.unit.test_mileage_purpose import _drive

    _drive(solo, start="2026-10-08T03:00:00+00:00")
    _notices(solo, today=FRIDAY)
    assert _drive_notices(solo) == []


# ---- the approver gets the month's email too (the owner, 2026-10-14): Carol,
# who approves Ruth's month, gets the same "ready" email with its link, and
# on that day it stands in for her "something is waiting".


@pytest.fixture
def grace(church):  # noqa: F811
    with Ledger.open(church["ledger_root"]) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET params_json = json_set(params_json, '$.month', '2026-09')"
        )
        ledger.conn.commit()
    return church


def _grace_notices(grace, today: str = "2026-10-14"):
    run(
        "grace",
        "routing",
        "notices",
        params={"today": today},
        ledger_dir=Path(grace["ledger_root"]).parent,
        tenants_root=grace["root"],
    )
    with Ledger.open(grace["ledger_root"]) as ledger:
        events = ledger.read_event_log()
    return [e["payload"] for e in events if e["event_type"] == NOTICE_EVENT]


def test_the_approver_gets_the_months_email_instead_of_waiting(grace):
    sent = _grace_notices(grace)
    carol = [(n["kind"], n["about"]) for n in sent if n["to"] == "carol-jennings"]
    assert ("month_ready", "ruth-hollis") in carol
    assert ("waiting", "") not in carol  # one email that day, with the link


def test_the_next_day_she_hears_it_still_waits(grace):
    _grace_notices(grace)
    sent = _grace_notices(grace, today="2026-10-15")
    days = [n["day"] for n in sent if n["to"] == "carol-jennings" and n["kind"] == "waiting"]
    assert days == ["2026-10-15"]
    readies = [n for n in sent if n["to"] == "carol-jennings" and n["kind"] == "month_ready"]
    assert len(readies) == 2  # Ruth's and the Bakers', once each


def test_nobody_hears_of_their_own_month(grace):
    sent = _grace_notices(grace)
    assert not [n for n in sent if n["kind"] == "month_ready" and n["to"] == n["about"]]


# ---- the morning brief, by role (chunk 12, board t-role-brief): a leader's
# "waiting" rolls up their people, by name, never what or how much; a
# missionary's own card names nobody.


def test_a_leaders_waiting_names_whose_things_wait_never_what(grace):
    _grace_notices(grace)
    sent = _grace_notices(grace, today="2026-10-15")
    carol = next(n for n in sent if n["to"] == "carol-jennings" and n["kind"] == "waiting")
    assert carol["people"] == ["ruth-hollis", "tom-and-jen-baker"]
    assert carol["more"] is False


def test_a_persons_own_card_names_nobody(solo):  # noqa: F811
    _notices(solo)
    daniel = next(n for n in _sent(solo) if n["kind"] == "waiting")
    assert daniel["to"] == "daniel-reyes" and daniel["people"] == []
    assert daniel["more"] is True  # his own month waits on him: something, about no one else


# ---- the board pack by email (t-pack-by-email; the owner, 2026-11-12): once a
# month's review closes, each person who looks over every month gets that
# month's statements as a PDF, once. The notice names the month, never a
# figure; the PDF is the board pack the books already make (c29).


def _approve(solo, month: str) -> None:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        for row in ledger.list_approvals("living-water", status="pending"):
            if row["action_type"] == MONTH_CARD and row["params"].get("month") == month:
                ledger.resolve_approval("living-water", int(row["id"]), "approved")


def _packs(solo) -> list[tuple[str, str]]:  # noqa: F811
    return sorted((n["to"], n["month"]) for n in _sent(solo) if n["kind"] == "board_pack")


def test_no_pack_while_the_months_yes_waits(solo):  # noqa: F811
    _notices(solo)
    assert _packs(solo) == []


def test_once_the_month_closes_whoever_looks_over_every_month_gets_its_pack(solo):  # noqa: F811
    _approve(solo, "2026-08")
    _approve(solo, "2026-09")
    _notices(solo)
    _notices(solo, today="2026-10-15")
    # Both look over every month: Linda as reviewer, and Daniel because a
    # solo ministry's months are all his (the same rule that lets him set its
    # budget). Only the newest closed month: a box that turns this on never
    # mails a backlog of old months.
    assert _packs(solo) == [("daniel-reyes", "2026-09"), ("linda-park", "2026-09")]
    pack = next(n for n in _sent(solo) if n["kind"] == "board_pack")
    assert set(pack) == {"id", "to", "kind", "about", "month", "day"}
    assert pack["about"] == ""


def test_a_month_still_waiting_holds_its_pack_even_when_an_older_one_closed(solo):  # noqa: F811
    _approve(solo, "2026-08")
    _notices(solo)
    assert _packs(solo) == []


def test_the_command_writes_each_packs_pdf_for_the_doorkeeper(solo, tmp_path, capsys):  # noqa: F811
    _approve(solo, "2026-08")
    _approve(solo, "2026-09")
    _notices(solo)
    packs = tmp_path / "packs"
    assert main(["notices", "living-water", "--days", "3650", "--packs", str(packs)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {(n["kind"], n["month"]) for n in lines} >= {("board_pack", "2026-09")}
    pdf = packs / "board-pack-2026-09.pdf"
    assert pdf.read_bytes().startswith(b"%PDF-")
    # Written once: a second run leaves the month's file as it was.
    before = pdf.stat().st_mtime_ns
    assert main(["notices", "living-water", "--days", "3650", "--packs", str(packs)]) == 0
    assert pdf.stat().st_mtime_ns == before
    assert sorted(p.name for p in packs.iterdir()) == ["board-pack-2026-09.pdf"]


def test_at_a_church_the_pack_goes_to_who_looks_over_every_month_not_a_missionary(grace):
    with Ledger.open(grace["ledger_root"]) as ledger:
        for row in ledger.list_approvals("grace", status="pending"):
            if row["action_type"] == MONTH_CARD:
                ledger.resolve_approval("grace", int(row["id"]), "approved")
    sent = _grace_notices(grace)
    packs = {n["to"] for n in sent if n["kind"] == "board_pack"}
    assert "carol-jennings" in packs
    assert "ruth-hollis" not in packs and "tom-and-jen-baker" not in packs
