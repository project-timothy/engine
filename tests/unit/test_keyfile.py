"""The hashed standing-key file (core/engine/keyfile.py): a tracker's device
id and a home-screen receipt link are each kept only as a hash."""

from __future__ import annotations

import stat

import pytest

from core.engine.keyfile import add_key, drop_keys, person_for_key, read_keys
from core.mileage.trackers import add_tracker, person_for


def test_a_key_is_kept_only_as_its_hash(tmp_path):
    path = tmp_path / "keys.toml"
    key = add_key(path, "ruth-hollis", header="Test keys.")
    assert key not in path.read_text()
    assert person_for_key(path, key) == "ruth-hollis"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_key_nobody_holds_names_nobody(tmp_path):
    path = tmp_path / "keys.toml"
    add_key(path, "ruth-hollis", header="Test keys.")
    assert person_for_key(path, "not-a-key") is None
    assert person_for_key(path, "") is None
    assert person_for_key(tmp_path / "missing.toml", "anything") is None


def test_only_one_retires_the_persons_older_keys_and_no_one_elses(tmp_path):
    path = tmp_path / "keys.toml"
    old = add_key(path, "ruth-hollis", header="Test keys.")
    theirs = add_key(path, "tom-and-jen-baker", header="Test keys.")
    new = add_key(path, "ruth-hollis", header="Test keys.", only_one=True)
    assert person_for_key(path, old) is None
    assert person_for_key(path, new) == "ruth-hollis"
    assert person_for_key(path, theirs) == "tom-and-jen-baker"


def test_dropping_a_person_retires_all_their_keys(tmp_path):
    path = tmp_path / "keys.toml"
    a = add_key(path, "ruth-hollis", header="Test keys.")
    b = add_key(path, "ruth-hollis", header="Test keys.")
    assert drop_keys(path, "ruth-hollis", header="Test keys.") == 2
    assert person_for_key(path, a) is None and person_for_key(path, b) is None
    assert read_keys(path) == {}
    assert drop_keys(path, "ruth-hollis", header="Test keys.") == 0


@pytest.mark.parametrize("who", ["", "Ruth Hollis", 'ruth"] = "x', "../ruth"])
def test_only_a_persons_id_can_hold_a_key(tmp_path, who):
    with pytest.raises(ValueError):
        add_key(tmp_path / "keys.toml", who, header="Test keys.")


def test_a_trackers_file_from_before_still_reads(tmp_path):
    """Boxes already hold trackers.toml under a [trackers] table."""
    token = add_tracker(tmp_path, "daniel-reyes")
    assert "[trackers]" in (tmp_path / "trackers.toml").read_text()
    assert person_for(tmp_path, token) == "daniel-reyes"
    again = add_tracker(tmp_path, "daniel-reyes", only_one=True)
    assert person_for(tmp_path, token) is None
    assert person_for(tmp_path, again) == "daniel-reyes"
