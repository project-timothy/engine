"""Drives from a phone's positions, keeping only miles and times (Robb's idea
2, decided 2026-10-13, option a).

A tracker app on the person's phone (Traccar Client, free) sends a position
every few seconds to their ministry's own box. Each one adds its step to the
drive under way and replaces the one before, so the box never holds more
than a person's latest position, and drops that the moment the drive ends
(``STOP_SECONDS`` without moving). An iPhone can go quiet mid-drive (Traccar
makes no promise about iOS reporting), so a phone that falls silent while
driving keeps its drive open up to ``GAP_SECONDS``; if it speaks again well
down the road, the straight line across the gap counts, short of the real
road but far closer than zero (the owner, 2026-10-15). A drive is whose, when it started and
ended, and how far: never where. Where it was for, the IRS log's purpose,
comes from the person's own words later. Standard library only."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

STOP_SECONDS = 5 * 60  # still this long, the drive is over
GAP_SECONDS = 30 * 60  # quiet this long after moving at speed, the drive is over
MOVING_MPH = 10.0  # at or over this when it went quiet, the phone was driving
BRIDGE_MIN_MILES = 0.5  # back this far from where it went quiet, it drove the gap
MIN_STEP_MILES = 0.01  # less is the phone's GPS wobbling, not the car moving
MIN_DRIVE_MILES = Decimal("0.1")
EARTH_MILES = 3958.8


@dataclass(frozen=True)
class Fix:
    """One position from the phone: when (epoch seconds) and where."""

    t: float
    lat: float
    lon: float


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_MILES * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Drive:
    person: str
    start: float
    end: float
    miles: Decimal

    def record(self) -> dict[str, str]:
        """The drive as the books keep it: no position in it."""

        def iso(t: float) -> str:
            return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")

        return {
            "person": self.person,
            "start": iso(self.start),
            "end": iso(self.end),
            "miles": str(self.miles),
        }


@dataclass
class _Open:
    start: float
    moved: float  # when it last moved
    heard: float  # when it last reported, moving or not
    last: Fix  # the only position held, replaced by each new one
    miles: float = 0.0
    mph: float = 0.0  # speed over the last step it moved

    def quiet_at_speed(self) -> bool:
        """Silent since it last moved, and it was driving then."""
        return self.heard == self.moved and self.mph >= MOVING_MPH

    def over(self, at: float) -> bool:
        return at - self.moved >= (GAP_SECONDS if self.quiet_at_speed() else STOP_SECONDS)


@dataclass
class Tracker:
    """Each person's drive under way, in memory only. A box restart ends the
    drive in progress without recording it; nothing about it is left."""

    _open: dict[str, _Open] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, person: str, fix: Fix) -> list[Drive]:
        """Take one position; returns the drive it ended, if a stop came first."""
        with self._lock:
            done: list[Drive] = []
            now = self._open.get(person)
            stopped = now is not None and fix.t - now.moved >= STOP_SECONDS
            if stopped and not _drove_the_gap(now, fix):
                done += self._close(person)
                now = None
            if now is None:
                self._open[person] = _Open(start=fix.t, moved=fix.t, heard=fix.t, last=fix)
                return done
            now.heard = fix.t
            step = haversine_miles((now.last.lat, now.last.lon), (fix.lat, fix.lon))
            if step >= MIN_STEP_MILES:
                if fix.t > now.last.t:
                    now.mph = step / (fix.t - now.last.t) * 3600
                now.miles += step
                now.moved = fix.t
                now.last = fix
            return done

    def flush(self, at: float) -> list[Drive]:
        """End every drive that has been still for ``STOP_SECONDS``, or quiet
        after driving for ``GAP_SECONDS``."""
        with self._lock:
            done: list[Drive] = []
            for person in [p for p, o in self._open.items() if o.over(at)]:
                done += self._close(person)
            return done

    def positions_held(self) -> int:
        with self._lock:
            return len(self._open)

    def _close(self, person: str) -> list[Drive]:
        now = self._open.pop(person)
        miles = Decimal(str(now.miles)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if miles < MIN_DRIVE_MILES:
            return []
        return [Drive(person, now.start, now.moved, miles)]


def _drove_the_gap(now: _Open, fix: Fix) -> bool:
    """A phone that went quiet while driving and speaks again within the gap,
    well away from where it fell silent, drove the gap: the drive goes on."""
    away = haversine_miles((now.last.lat, now.last.lon), (fix.lat, fix.lon))
    return now.quiet_at_speed() and fix.t - now.moved < GAP_SECONDS and away >= BRIDGE_MIN_MILES


__all__ = ["GAP_SECONDS", "STOP_SECONDS", "Drive", "Fix", "Tracker", "haversine_miles"]
