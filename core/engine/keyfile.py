"""A tenant file of standing keys, each kept only as its SHA-256 beside the
person it belongs to: a phone tracker's device id (core/mileage/trackers.py)
and a person's home-screen receipt link (core/tools/receipts.py). The key
itself is shown once and never written. Taking a person's line out of the
file retires their key. Standard library only."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import tomllib
from pathlib import Path

_PERSON = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def read_keys(path: Path) -> dict[str, str]:
    """Each key's SHA-256 -> the person it belongs to; empty when the file
    is missing or unreadable."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    held = data.get("keys", data.get("trackers", {}))
    return {str(k): str(v) for k, v in held.items()} if isinstance(held, dict) else {}


def _write(path: Path, held: dict[str, str], header: str, table: str) -> None:
    body = f"# {header} Never the key itself.\n[{table}]\n"
    body += "".join(f'"{k}" = "{v}"\n' for k, v in sorted(held.items()))
    path = Path(path)
    part = path.with_name(path.name + ".part")
    part.write_text(body, encoding="utf-8")
    part.chmod(0o600)
    part.replace(path)


def add_key(
    path: Path, person: str, *, header: str, table: str = "keys", only_one: bool = False
) -> str:
    """A new key for ``person``, shown once and kept only as its hash; with
    ``only_one``, the person's older keys in the file stop working."""
    if not _PERSON.match(person):
        raise ValueError(f"{person!r} is not a person's id")
    key = secrets.token_urlsafe(24)
    held = read_keys(path)
    if only_one:
        held = {k: v for k, v in held.items() if v != person}
    _write(path, {**held, _digest(key): person}, header, table)
    return key


def drop_keys(path: Path, person: str, *, header: str, table: str = "keys") -> int:
    """Retire every key ``person`` holds in the file; how many there were."""
    held = read_keys(path)
    kept = {k: v for k, v in held.items() if v != person}
    if len(kept) != len(held):
        _write(path, kept, header, table)
    return len(held) - len(kept)


def person_for_key(path: Path, key: str) -> str | None:
    """The person a key belongs to, or None for a key nobody holds."""
    if not key:
        return None
    want = _digest(key)
    for digest, person in read_keys(path).items():
        if hmac.compare_digest(digest, want):
            return person
    return None


__all__ = ["add_key", "drop_keys", "person_for_key", "read_keys"]
