"""The ``unreadable`` test helper (public issue #9): three regressions make a
folder unreadable with ``chmod``, and root ignores permissions, so as root the
denial never happened and the tests failed for a reason that is not theirs.
The helper proves the denial holds before the test relies on it, and skips
with the reason when it does not, whichever user (or capability) runs it."""

from __future__ import annotations

import os

import pytest

from conftest import unreadable


def test_the_folder_is_unreadable_inside_and_restored_after(tmp_path, monkeypatch):
    d = tmp_path / "Statements"
    d.mkdir()
    real_listdir = os.listdir

    def _denied(path="."):
        if str(path) == str(d):
            raise PermissionError(13, "Permission denied", str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _denied)  # a host where the chmod holds
    with unreadable(d):
        with pytest.raises(PermissionError):
            os.listdir(d)
    monkeypatch.undo()
    assert os.listdir(d) == []  # permissions restored for cleanup


def test_a_user_the_chmod_cannot_stop_skips_with_the_reason(tmp_path, monkeypatch):
    d = tmp_path / "Statements"
    d.mkdir()
    monkeypatch.setattr(os, "listdir", lambda path=".": [])  # root reads anyway
    with pytest.raises(pytest.skip.Exception, match="ignores file permissions"):
        with unreadable(d):
            pass
    monkeypatch.undo()
    assert os.listdir(d) == []  # restored even on the skip
