"""Which phone is whose: ``trackers.toml`` in the tenant's folder holds each
tracker token's SHA-256 and the person it belongs to, never the token. The
token is the device id the tracker app sends with every position, set by
the setup link (core/onboarding/person.py) or typed once. A phone is known
only by it. Standard library only (the hashed-key file: core/engine/keyfile.py)."""

from __future__ import annotations

from pathlib import Path

from ..engine.keyfile import add_key, person_for_key

FILE = "trackers.toml"
_HEADER = "Tracker tokens' SHA-256 -> person (core/mileage/trackers.py)."


def add_tracker(directory: Path, person: str, *, only_one: bool = False) -> str:
    """A new token for ``person``'s phone; shown once, kept only as its hash.
    With ``only_one`` (setup's own link), their earlier phone stops counting."""
    return add_key(
        Path(directory) / FILE, person, header=_HEADER, table="trackers", only_one=only_one
    )


def person_for(directory: Path, token: str) -> str | None:
    return person_for_key(Path(directory) / FILE, token)
