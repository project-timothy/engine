"""Which phone is whose: ``trackers.toml`` in the tenant's folder holds each
tracker token's SHA-256 and the person it belongs to, never the token. The
token is the device id the person types into the tracker app once, so a
phone is known only by it. Standard library only."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import tomllib
from pathlib import Path

FILE = "trackers.toml"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read(directory: Path) -> dict[str, str]:
    try:
        data = tomllib.loads((Path(directory) / FILE).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    held = data.get("trackers", {})
    return {str(k): str(v) for k, v in held.items()} if isinstance(held, dict) else {}


def add_tracker(directory: Path, person: str) -> str:
    """A new token for ``person``'s phone; shown once, kept only as its hash."""
    token = secrets.token_urlsafe(24)
    held = {**_read(directory), _digest(token): person}
    body = "# Tracker tokens' SHA-256 -> person (core/mileage/trackers.py). Never the token.\n"
    body += "[trackers]\n" + "".join(f'"{k}" = "{v}"\n' for k, v in sorted(held.items()))
    path = Path(directory) / FILE
    part = path.with_name(FILE + ".part")
    part.write_text(body, encoding="utf-8")
    part.chmod(0o600)
    part.replace(path)
    return token


def person_for(directory: Path, token: str) -> str | None:
    want = _digest(token)
    for digest, person in _read(directory).items():
        if hmac.compare_digest(digest, want):
            return person
    return None
