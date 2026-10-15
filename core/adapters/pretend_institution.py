"""A pretend institution: takes claims the way AGWM's interface will, until
its documentation is in hand (Robb's idea 1, 2026-10-13).

It keeps what it was sent in ``claims.json`` in its folder and answers each
claim with a reference. ``go_down`` leaves a ``down`` marker in the folder,
so a practice box or a test can show what a missionary sees when the
institution isn't answering. Standard library only."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from ..contracts.institution import Claim, InstitutionDown, Taken


class PretendInstitution:
    def __init__(self, store: Path) -> None:
        self.store = Path(store)
        self.store.mkdir(parents=True, exist_ok=True)
        self._file = self.store / "claims.json"
        self._down = self.store / "down"

    def sent(self) -> list[dict]:
        return json.loads(self._file.read_text(encoding="utf-8")) if self._file.is_file() else []

    def go_down(self) -> None:
        self._down.write_text("down\n", encoding="utf-8")

    def come_back(self) -> None:
        self._down.unlink(missing_ok=True)

    def submit(self, claims: Sequence[Claim]) -> list[Taken]:
        if self._down.exists():
            raise InstitutionDown("the pretend institution is down")
        kept = self.sent()
        known = {c["receipt"]: c["reference"] for c in kept}
        taken = []
        for claim in claims:
            if claim.receipt in known:  # sent before: the first answer stands
                taken.append(Taken(claim.receipt, known[claim.receipt]))
                continue
            reference = f"PRETEND-{len(kept) + 1:04d}"
            kept.append({**asdict(claim), "reference": reference, "status": "received"})
            taken.append(Taken(claim.receipt, reference))
        part = self._file.with_name(self._file.name + ".part")
        part.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
        part.replace(self._file)
        return taken

    def status(self, reference: str) -> str:
        return next((c["status"] for c in self.sent() if c["reference"] == reference), "")
