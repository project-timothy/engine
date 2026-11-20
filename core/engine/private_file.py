"""Files a box keeps beside its books, private to its own account: written
whole (a crash leaves the old file), the folders 0700 and the file 0600.
Shared by Tim's conversation store (core/talk/store.py) and what a room
keeps across a nap (core/tools/receipts.ReceiptDesk, the month page's key;
shot 4, card H3). Beside the ledger, never inside it: every run commits
what sits under the ledger's root, and these are meant to go."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

STATE_DIR = "state"
_PLAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def plain_name(given: str) -> str:
    """A file or folder name for an id: the id when it is a plain slug, else
    a hash of it. An id never becomes a path."""
    if _PLAIN.fullmatch(given):
        return given
    return hashlib.sha256(given.encode("utf-8")).hexdigest()[:32]


def keep(path: Path, text: str) -> None:
    """Write ``text`` whole (a crash leaves the old file), private to the box's
    own account: the folders 0700, the file 0600."""
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    for made in (folder.parent, folder):
        made.chmod(0o700)
    scratch = path.with_suffix(".tmp")
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(scratch, path)
    path.chmod(0o600)


def state_folder(ledger_root: str | Path, tenant: str) -> Path:
    """Where a tenant's box keeps what must outlive its process."""
    return Path(ledger_root).parent / STATE_DIR / plain_name(tenant)


__all__ = ["STATE_DIR", "keep", "plain_name", "state_folder"]
