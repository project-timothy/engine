"""Drives from a phone's positions, keeping only miles and times (Robb's idea
2, decided 2026-10-13, option a).

A tracker app on the person's phone (Traccar Client, free) sends a position
every few seconds to their ministry's own box. Each one adds its step to the
drive under way and replaces the one before, so the box never holds more
than a person's latest position, and drops that the moment the drive ends
(``STOP_SECONDS`` without moving). A drive is whose, when it started and
ended, and how far: never where. Where it was for, the IRS log's purpose,
comes from the person's own words later. Standard library only."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

STOP_SECONDS = 5 * 60  # still this long, the drive is over
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
    last: Fix  # the only position held, replaced by each new one
    miles: float = 0.0


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
            if now is not None and fix.t - now.moved >= STOP_SECONDS:
                done += self._close(person)
                now = None
            if now is None:
                self._open[person] = _Open(start=fix.t, moved=fix.t, last=fix)
                return done
            step = haversine_miles((now.last.lat, now.last.lon), (fix.lat, fix.lon))
            if step >= MIN_STEP_MILES:
                now.miles += step
                now.moved = fix.t
                now.last = fix
            return done

    def flush(self, at: float) -> list[Drive]:
        """End every drive that has been still for ``STOP_SECONDS``."""
        with self._lock:
            done: list[Drive] = []
            for person in [p for p, o in self._open.items() if at - o.moved >= STOP_SECONDS]:
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


__all__ = ["STOP_SECONDS", "Drive", "Fix", "Tracker", "haversine_miles"]
