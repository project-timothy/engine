"""The box's end of a phone tracker (core/mileage): one position in, from
the tracker app's OsmAnd request (id, lat, lon, timestamp), and each drive
that ends into the books through the expenses lane's drive job."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs

from . import Fix, Tracker
from .trackers import person_for


class TrackDoor:
    """With no tracker (a box that keeps no drives), every call does nothing."""

    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        ledger_root: Path,
        tracker: Tracker | None,
    ) -> None:
        self.on = tracker is not None
        self.tenant = tenant
        self.tenants_root = Path(tenants_root) if tenants_root else None
        self.ledger_root = Path(ledger_root)
        self.tracker = tracker

    def take(self, query: str) -> None:
        """One position. An unknown id, or a request that isn't a position,
        is dropped without a word: the reply never tells a stranger anything."""
        from ..engine.config import tenant_dir

        if self.tracker is None:
            return
        q = {k: v[-1] for k, v in parse_qs(query).items()}
        person = person_for(
            tenant_dir(self.tenant, tenants_root=self.tenants_root), q.get("id", "")
        )
        if person is None:
            return
        try:
            stamp = float(q.get("timestamp") or time.time())
            fix = Fix(stamp / 1000 if stamp > 1e12 else stamp, float(q["lat"]), float(q["lon"]))
        except (KeyError, ValueError):
            return
        self._record(self.tracker.add(person, fix))

    def flush(self, at: float | None = None) -> None:
        """End every drive that has stood still long enough (the minute timer)."""
        if self.tracker is None:
            return
        self._record(self.tracker.flush(time.time() if at is None else at))

    def _record(self, drives: list) -> None:
        from ..engine.runner import run

        for drive in drives:
            run(
                self.tenant,
                "expenses",
                "drive",
                params=drive.record(),
                ledger_dir=self.ledger_root.parent,
                tenants_root=self.tenants_root,
            )
