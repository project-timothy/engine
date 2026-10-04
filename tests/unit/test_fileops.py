"""The verified-placement contract (honesty audit 2026-09-03, PR 2).

A hash of None means cannot verify: defer, never adopt, never suffix, never
overwrite. Identical content is adopted; different content is suffixed; a
real copy is read back; shadow writes nothing.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from core.engine import fileops
from core.engine.fileops import CannotVerify, CopyMismatch, place_bytes, place_copy
from core.engine.guard import ProtectedSurfaceError, WriteGuard


def _cloud_only_for(*paths):
    """A hash stand-in: None (cloud-only) for exactly these paths."""
    targets = {Path(p) for p in paths}

    def _hash(path: Path):
        if Path(path) in targets:
            return None
        return fileops.content_hash(path)

    return _hash


def _setup(tmp_path):
    src = tmp_path / "in" / "doc.pdf"
    src.parent.mkdir()
    src.write_bytes(b"%PDF one")
    return src, tmp_path / "out" / "doc.pdf"


def test_fresh_destination_is_copied_and_read_back(tmp_path):
    src, dest = _setup(tmp_path)

    placed = place_copy(src, dest, guard=WriteGuard([]), shadow=False)

    assert placed.dest == dest and placed.copied and not placed.adopted and placed.present
    assert dest.read_bytes() == b"%PDF one"
    assert src.exists()  # a copy, never a move


def test_identical_content_is_adopted_not_recopied(tmp_path):
    src, dest = _setup(tmp_path)
    dest.parent.mkdir()
    dest.write_bytes(b"%PDF one")
    before = dest.stat().st_mtime_ns

    placed = place_copy(src, dest, guard=WriteGuard([]), shadow=False)

    assert placed.adopted and not placed.copied and placed.present
    assert dest.stat().st_mtime_ns == before


def test_different_content_suffixes_as_many_times_as_it_takes(tmp_path):
    src, dest = _setup(tmp_path)
    dest.parent.mkdir()
    dest.write_bytes(b"other 1")
    (dest.parent / "doc (2).pdf").write_bytes(b"other 2")

    placed = place_copy(src, dest, guard=WriteGuard([]), shadow=False)

    assert placed.dest.name == "doc (3).pdf" and placed.copied
    assert (dest.parent / "doc (2).pdf").read_bytes() == b"other 2"  # never overwritten


def test_custom_suffix_format(tmp_path):
    src, dest = _setup(tmp_path)
    dest.parent.mkdir()
    dest.write_bytes(b"other")

    placed = place_copy(
        src, dest, guard=WriteGuard([]), shadow=False, suffix_fmt="{stem}_{n}{suffix}"
    )

    assert placed.dest.name == "doc_2.pdf"


def test_cloud_only_destination_is_cannot_verify_not_a_collision(tmp_path):
    src, dest = _setup(tmp_path)
    dest.parent.mkdir()
    dest.write_bytes(b"placeholder stand-in")

    with pytest.raises(CannotVerify) as info:
        place_copy(src, dest, guard=WriteGuard([]), shadow=False, hash_fn=_cloud_only_for(dest))

    assert info.value.side == "destination" and info.value.errno == errno.EDEADLK
    assert not (dest.parent / "doc (2).pdf").exists()  # no duplicate on a guess


def test_cloud_only_source_is_cannot_verify(tmp_path):
    src, dest = _setup(tmp_path)

    with pytest.raises(CannotVerify) as info:
        place_copy(src, dest, guard=WriteGuard([]), shadow=False, hash_fn=_cloud_only_for(src))

    assert info.value.side == "source"
    assert not dest.exists()


def test_two_cloud_only_sides_never_adopt(tmp_path):
    """The audit's 02-F6 shape: None == None used to adopt a file never compared."""
    src, dest = _setup(tmp_path)
    dest.parent.mkdir()
    dest.write_bytes(b"who knows")

    with pytest.raises(CannotVerify):
        place_copy(src, dest, guard=WriteGuard([]), shadow=False, hash_fn=lambda p: None)


def test_copy_that_reads_back_wrong_is_a_mismatch_and_leaves_no_partial(tmp_path, monkeypatch):
    src, dest = _setup(tmp_path)

    def _short_copy(s, d):
        Path(d).write_bytes(b"%PD")  # a truncated write on a sync mount

    monkeypatch.setattr(fileops.shutil, "copy2", _short_copy)

    with pytest.raises(CopyMismatch):
        place_copy(src, dest, guard=WriteGuard([]), shadow=False)

    assert not dest.exists()


def test_shadow_writes_nothing_and_names_the_destination(tmp_path):
    src, dest = _setup(tmp_path)

    placed = place_copy(src, dest, guard=WriteGuard([]), shadow=True)

    assert placed.dest == dest and not placed.copied and not placed.adopted and not placed.present
    assert not dest.exists()


def test_protected_destination_is_refused_even_in_shadow(tmp_path):
    src, dest = _setup(tmp_path)
    guard = WriteGuard([dest.parent])

    with pytest.raises(ProtectedSurfaceError):
        place_copy(src, dest, guard=guard, shadow=True)


def test_place_bytes_adopts_suffixes_and_reads_back(tmp_path):
    dest = tmp_path / "landing" / "a.pdf"
    guard = WriteGuard([])

    first = place_bytes(b"one", dest, guard=guard, shadow=False)
    again = place_bytes(b"one", dest, guard=guard, shadow=False)
    other = place_bytes(b"two", dest, guard=guard, shadow=False)

    assert first.copied and first.dest == dest
    assert again.adopted and again.dest == dest
    assert other.copied and other.dest.name == "a (2).pdf"


def test_the_collision_suffix_is_readable_by_the_rules_that_must_undo_it():
    """A name this module suffixed is still the same document, and a reader
    that has to recognize it (the AP inline-graphic pre-filter) asks here
    instead of re-spelling the format. Only the default convention is
    promised: a caller passing its own ``suffix_fmt`` owns reading it back."""
    from core.engine.fileops import COLLISION_SUFFIX_FMT, strip_collision_suffix

    assert COLLISION_SUFFIX_FMT == "{stem} ({n}){suffix}"
    assert strip_collision_suffix("image001 (2)") == "image001"
    assert strip_collision_suffix("image001 (19)") == "image001"
    assert strip_collision_suffix("image001") == "image001"
    # only a trailing count, and only one: nothing else is the module's doing
    assert strip_collision_suffix("Invoice (final)") == "Invoice (final)"
    assert strip_collision_suffix("Inv (2) draft") == "Inv (2) draft"
    assert strip_collision_suffix("doc (2) (3)") == "doc (2)"


def test_the_default_suffix_is_the_named_convention(tmp_path):
    dest = tmp_path / "landing" / "image001.png"
    guard = WriteGuard([])

    place_bytes(b"one", dest, guard=guard, shadow=False)
    second = place_bytes(b"two", dest, guard=guard, shadow=False)

    from core.engine.fileops import strip_collision_suffix

    assert second.dest.name == "image001 (2).png"
    assert strip_collision_suffix(second.dest.stem) == dest.stem


def test_place_bytes_cloud_only_destination_is_cannot_verify(tmp_path, monkeypatch):
    dest = tmp_path / "landing" / "a.pdf"
    dest.parent.mkdir()
    dest.write_bytes(b"stand-in")
    monkeypatch.setattr(fileops, "content_hash", lambda p, algo="md5": None)

    with pytest.raises(CannotVerify):
        place_bytes(b"one", dest, guard=WriteGuard([]), shadow=False)
