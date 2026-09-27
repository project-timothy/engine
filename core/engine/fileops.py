"""Verified file placement: one contract for every copy into a cloud tree.

Honesty audit 2026-09-03 (issue #170, PR 2). The engine copies files into
cloud-synced folders (File Provider mounts with dataless placeholders) from
five places (AP filing, W-9 filing, timesheets, expenses, mail landing) and
each grew its own never-overwrite loop. Two of them treated a hash that came back ``None``
(a dataless cloud placeholder, ``EDEADLK``) as a value: ``None == None``
adopted a file that was never compared; ``None != x`` filed a duplicate.
None of them read the destination back before claiming "filed".

The contract here, for every caller:

- a hash of ``None`` on EITHER side means CANNOT VERIFY: :class:`CannotVerify`
  is raised, the caller defers with an anomaly and retries on a later run;
  never adopt, never suffix, never overwrite on a guess;
- identical content already at a candidate destination is adopted, no copy;
- different content moves to the next suffixed name, as many as it takes;
- a real copy is read back and its hash must equal the source's, else
  :class:`CopyMismatch` (the partial destination is removed so the retry is
  clean);
- shadow writes nothing and returns the destination it would have used;
- the write guard is consulted on the destination before anything else.
"""

from __future__ import annotations

import errno
import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HashFn = Callable[[Path], str | None]

# The one collision-suffix convention every placement in the engine writes.
# It is named here, and read back here, because a rule that decides something
# from a FILENAME is deciding about a name this module may have written: a
# deterministic name rule that did not know the convention stopped matching
# the second copy of a file it had matched the first time. A caller that
# passes its own ``suffix_fmt`` owns reading that one back.
COLLISION_SUFFIX_FMT = "{stem} ({n}){suffix}"
_COLLISION_SUFFIX = re.compile(r" \(\d+\)$")


def strip_collision_suffix(stem: str) -> str:
    """``stem`` without the collision suffix a placement here would append.

    The suffix belongs to the destination folder, not to the document: two
    saves of one email logo into a taken name are the same logo. Only a single
    trailing count is the module's doing, so a name whose own text ends in
    parentheses is returned untouched.
    """
    return _COLLISION_SUFFIX.sub("", stem)


class CannotVerify(OSError):
    """A file whose content cannot be read here (a cloud-only placeholder):
    nothing about it may be assumed. Defer and retry."""

    def __init__(self, path: Path, side: str):
        super().__init__(errno.EDEADLK, f"cannot verify {side} (cloud-only placeholder)", str(path))
        self.path = path
        self.side = side


class CopyMismatch(OSError):
    """The destination read back with a different hash than the source."""

    def __init__(self, src: Path, dest: Path):
        super().__init__(errno.EIO, "copy read back with different content", str(dest))
        self.src = src
        self.dest = dest


@dataclass(frozen=True)
class Placed:
    dest: Path
    copied: bool  # a real, verified copy happened
    adopted: bool  # identical content was already there; nothing written

    @property
    def present(self) -> bool:
        """The content is verifiably at ``dest`` (copied or adopted)."""
        return self.copied or self.adopted


def content_hash(path: Path, *, algo: str = "md5") -> str | None:
    """Hex digest of the file, or ``None`` for a cloud-only placeholder.

    ``OSError(EDEADLK)`` is how a dataless File Provider placeholder answers
    a headless read (docs/lessons.md, "A cloud placeholder is not here yet"). Every other error
    propagates.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        if exc.errno == errno.EDEADLK:
            return None
        raise
    return hashlib.new(algo, data).hexdigest()


def _candidates(dest: Path, suffix_fmt: str):
    yield dest
    n = 2
    while True:
        yield dest.with_name(suffix_fmt.format(stem=dest.stem, n=n, suffix=dest.suffix))
        n += 1


def place_copy(
    src: Path,
    dest: Path,
    *,
    guard,
    shadow: bool,
    hash_fn: HashFn | None = None,
    suffix_fmt: str = COLLISION_SUFFIX_FMT,
) -> Placed:
    """Copy ``src`` to ``dest`` (or the first free suffixed name) under the
    contract in the module docstring. ``hash_fn`` must return ``None`` for a
    file it cannot read (default: :func:`content_hash`)."""
    hash_fn = hash_fn or content_hash
    guard.check_write(dest)
    src_hash = hash_fn(src)
    if src_hash is None:
        raise CannotVerify(src, "source")
    for candidate in _candidates(dest, suffix_fmt):
        if not candidate.exists():
            target = candidate
            break
        existing = hash_fn(candidate)
        if existing is None:
            raise CannotVerify(candidate, "destination")
        if existing == src_hash:
            return Placed(dest=candidate, copied=False, adopted=True)
    guard.check_write(target)
    if shadow:
        return Placed(dest=target, copied=False, adopted=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    if hash_fn(target) != src_hash:
        try:
            target.unlink()
        except OSError:
            pass
        raise CopyMismatch(src, target)
    return Placed(dest=target, copied=True, adopted=False)


def place_bytes(
    body: bytes,
    dest: Path,
    *,
    guard,
    shadow: bool,
    suffix_fmt: str = COLLISION_SUFFIX_FMT,
) -> Placed:
    """Write ``body`` to ``dest`` (or the first free suffixed name) under the
    same contract: adopt identical content, suffix different content, read
    the write back, never guess about a placeholder."""
    guard.check_write(dest)
    body_hash = hashlib.md5(body).hexdigest()
    for candidate in _candidates(dest, suffix_fmt):
        if not candidate.exists():
            target = candidate
            break
        existing = content_hash(candidate)
        if existing is None:
            raise CannotVerify(candidate, "destination")
        if existing == body_hash:
            return Placed(dest=candidate, copied=False, adopted=True)
    guard.check_write(target)
    if shadow:
        return Placed(dest=target, copied=False, adopted=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    if content_hash(target) != body_hash:
        try:
            target.unlink()
        except OSError:
            pass
        raise CopyMismatch(dest, target)
    return Placed(dest=target, copied=True, adopted=False)
