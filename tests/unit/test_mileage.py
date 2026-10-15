"""Mileage from the phone, keeping only miles and times (Robb's idea 2,
decided 2026-10-13, option a): a tracker app on the person's phone sends
positions to their ministry's own box; the box adds each step to the drive
under way and keeps nothing but the latest position, which goes the moment
the drive ends. What lands in the books is a drive: whose, when it started
and ended, and how many miles. Never where."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.engine.runner import run
from core.ledger import Ledger
from core.mileage import STOP_SECONDS, Fix, Tracker, haversine_miles
from core.mileage.trackers import add_tracker, person_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

# Kathmandu, a few streets apart: about 0.69 miles per 0.01 degree of latitude.
START = (27.7000, 85.3200)


def _north(fix: tuple[float, float], degrees: float) -> tuple[float, float]:
    return (fix[0] + degrees, fix[1])


def _drive(tracker: Tracker, person: str, t0: float, legs: int, step: float = 0.002):
    at = START
    closed = []
    for i in range(legs + 1):
        closed += tracker.add(person, Fix(t0 + 60 * i, *at))
        at = _north(at, step)
    return closed


def test_miles_between_two_points():
    assert haversine_miles((27.70, 85.32), (27.71, 85.32)) == pytest.approx(0.69, abs=0.01)


def test_a_drive_is_whose_when_and_how_far_never_where():
    tracker = Tracker()
    _drive(tracker, "daniel-reyes", 1_000_000.0, legs=10)  # ten minutes north
    [drive] = tracker.flush(1_000_000.0 + 600 + STOP_SECONDS + 1)
    assert drive.person == "daniel-reyes"
    assert drive.miles == Decimal("1.4")  # 0.02 degrees of latitude, to a tenth
    assert drive.end - drive.start == 600
    assert set(drive.record()) == {"person", "start", "end", "miles"}


def test_no_position_outlives_its_drive():
    tracker = Tracker()
    _drive(tracker, "daniel-reyes", 1_000_000.0, legs=5)
    assert tracker.positions_held() == 1  # only the latest, while the drive runs
    tracker.flush(1_000_000.0 + 300 + STOP_SECONDS + 1)
    assert tracker.positions_held() == 0


def test_standing_still_is_not_a_drive():
    tracker = Tracker()
    for i in range(20):  # GPS wobble in a parking lot
        tracker.add("daniel-reyes", Fix(1_000_000.0 + 30 * i, 27.70 + (i % 2) * 0.00005, 85.32))
    assert tracker.flush(1_000_000.0 + 600 + STOP_SECONDS + 1) == []


def test_a_stop_ends_the_drive_and_the_next_starts_fresh():
    tracker = Tracker()
    _drive(tracker, "daniel-reyes", 1_000_000.0, legs=5)
    later = _drive(tracker, "daniel-reyes", 1_000_000.0 + 300 + STOP_SECONDS + 60, legs=5)
    assert len(later) == 1 and later[0].miles == Decimal("0.7")


def test_a_phone_is_known_only_by_its_token(tmp_path):
    token = add_tracker(tmp_path, "daniel-reyes")
    assert person_for(tmp_path, token) == "daniel-reyes"
    assert person_for(tmp_path, "guess") is None
    assert token not in (tmp_path / "trackers.toml").read_text()  # only its hash is kept


def test_a_drive_lands_in_the_books_as_miles_and_times(solo):  # noqa: F811
    tracker = Tracker()
    _drive(tracker, "daniel-reyes", 1_791_900_000.0, legs=10)
    [drive] = tracker.flush(1_791_900_000.0 + 600 + STOP_SECONDS + 1)
    done = run(
        "living-water",
        "expenses",
        "drive",
        params={k: str(v) for k, v in drive.record().items()},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )
    assert done.status == "ok"
    with Ledger.open(solo["ledger_root"]) as ledger:
        [event] = [e for e in ledger.read_event_log() if e["event_type"] == "mileage.drive"]
    payload = event["payload"]
    assert payload["miles"] == "1.4" and payload["person"] == "Daniel Reyes"
    for word in ("lat", "lon", "position", "place"):
        assert word not in json.dumps(payload)


# ---- the box's door ---------------------------------------------------------------

import threading  # noqa: E402
import urllib.request  # noqa: E402

from core.engine.config import tenant_dir  # noqa: E402
from core.tools.mcp_http import TokenFile, make_server  # noqa: E402


def _drives(solo) -> list[dict]:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        return [e["payload"] for e in ledger.read_event_log() if e["event_type"] == "mileage.drive"]


def test_the_phone_reports_to_the_box_and_the_box_keeps_a_drive(solo, tmp_path):  # noqa: F811
    token = add_tracker(tenant_dir("living-water", tenants_root=solo["root"]), "daniel-reyes")
    server = make_server(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource="https://tim.example.org/mcp",
        authorization_servers=["https://signin.example.org"],
        port=0,
        tracker=Tracker(),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/track"
    try:
        at, t0 = START, 1_791_900_000
        for i in range(11):
            url = f"{base}?id={token}&lat={at[0]}&lon={at[1]}&timestamp={t0 + 60 * i}"
            with urllib.request.urlopen(urllib.request.Request(url, method="POST")) as r:
                assert r.status == 200
            at = _north(at, 0.002)
        with urllib.request.urlopen(f"{base}?id=guess&lat=1&lon=1&timestamp={t0}") as r:
            assert r.status == 200  # a stranger hears nothing different
        server.flush_drives(t0 + 600 + STOP_SECONDS + 1)
    finally:
        server.shutdown()
    [drive] = _drives(solo)
    assert drive["person"] == "Daniel Reyes" and drive["miles"] == "1.4"
