"""A room, packed to move (shot 4, card H3).

A small ministry's room on a shared house is two folders: ``church/`` (its
books and settings) and ``box/`` (its doorkeeper's database and secret).
:func:`pack` folds them into one file with a manifest: the SHA-256 of every
file, and the row count of every table in every database, each database
copied consistently (SQLite's own backup) even while the room is awake.
:func:`restore` unpacks it somewhere new only after every hash and every
count matches the manifest, and never over a room that exists. The house's
scripts make the room's account, units, and address around it.

    engine room pack <room folder> --out <file>
    engine room restore <file> --into <new room folder>
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MANIFEST = "room-manifest.json"
_DATABASES = (".sqlite3", ".db")


class PackError(Exception):
    """A pack that doesn't match its manifest, or a restore that would land
    somewhere it mustn't."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _counts(path: Path) -> dict[str, int]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        counts = {}
        for name in sorted(names):
            quoted = '"' + name.replace('"', '""') + '"'
            query = f"SELECT count(*) FROM {quoted}"  # noqa: S608  (a name from the file's own catalog, quoted as one)
            counts[name] = db.execute(query).fetchone()[0]
        return counts
    finally:
        db.close()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix in _DATABASES:
        source, target = sqlite3.connect(src), sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    else:
        shutil.copy2(src, dst)


def _manifest_of(top: Path) -> dict:
    files: dict[str, str] = {}
    tables: dict[str, dict[str, int]] = {}
    for path in sorted(p for p in top.rglob("*") if p.is_file()):
        rel = path.relative_to(top).as_posix()
        if rel == MANIFEST:
            continue
        files[rel] = _sha(path)
        if path.suffix in _DATABASES:
            tables[rel] = _counts(path)
    return {"files": files, "tables": tables}


def pack(room: Path, out: Path) -> dict:
    """Fold ``room`` into ``out`` (a .tgz, 0600) with its manifest; returns it."""
    room = Path(room)
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "room"
        for path in sorted(p for p in room.rglob("*") if p.is_file()):
            if path.name.endswith(("-wal", "-shm", "-journal")):
                continue  # a database's own side files: its backup holds them
            _copy(path, copy / path.relative_to(room))
        manifest = _manifest_of(copy) | {"packed_at": datetime.now(UTC).isoformat()}
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            data = json.dumps(manifest, indent=1, sort_keys=True).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            for rel in manifest["files"]:
                tar.add(copy / rel, arcname=rel)
    return manifest


def _safe(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts


def restore(packed: Path, into: Path) -> dict:
    """Unpack ``packed`` as the new room folder ``into``, only after every
    file's hash and every table's count match its manifest; returns what
    was restored. ``into`` must not exist yet."""
    into = Path(into)
    if into.exists():
        raise PackError(f"{into} is already a room; a restore never lands on one")
    with tempfile.TemporaryDirectory(dir=into.parent if into.parent.exists() else None) as scratch:
        top = Path(scratch) / "room"
        top.mkdir()
        with tarfile.open(packed, "r:gz") as tar:
            members = tar.getmembers()
            bad = [m.name for m in members if not _safe(m.name) or not (m.isfile() or m.isdir())]
            if bad:
                raise PackError(f"the pack names paths outside a room: {', '.join(bad)}")
            tar.extractall(top, filter="data")
        try:
            said = json.loads((top / MANIFEST).read_text())
        except (OSError, ValueError) as exc:
            raise PackError("the pack has no manifest") from exc
        (top / MANIFEST).unlink()
        found = _manifest_of(top)
        for rel in sorted(set(said["files"]) | set(found["files"])):
            if said["files"].get(rel) != found["files"].get(rel):
                raise PackError(f"{rel} doesn't match the pack's manifest")
        if said["tables"] != found["tables"]:
            raise PackError("a table's count doesn't match the pack's manifest")
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(top, into)
    os.chmod(into, 0o700)
    return found


__all__ = ["MANIFEST", "PackError", "pack", "restore"]
