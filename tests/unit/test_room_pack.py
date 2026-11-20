"""A room moves between houses (shot 4, card H3): ``engine room pack`` folds
a room's folders into one file with a manifest (every file's hash, every
table's row count), and ``restore`` refuses to unpack anything that doesn't
match it, so the counts after a move are the counts before."""

import json
import sqlite3
import tarfile

import pytest

from core.engine import room


def _room(tmp_path):
    r = tmp_path / "blue-heron-42"
    (r / "church" / "ledger" / "hope").mkdir(parents=True)
    (r / "box").mkdir()
    db = sqlite3.connect(r / "church" / "ledger" / "hope" / "ledger.sqlite3")
    db.executescript("CREATE TABLE events (id INTEGER); INSERT INTO events VALUES (1), (2), (3);")
    db.commit()
    db.close()
    door = sqlite3.connect(r / "box" / "doorkeeper.db")
    door.executescript("CREATE TABLE credentials (id TEXT); INSERT INTO credentials VALUES ('a');")
    door.commit()
    door.close()
    (r / "church" / "box.json").write_text('{"slug": "hope"}')
    return r


def test_a_pack_carries_every_count_and_a_restore_matches_them(tmp_path):
    r = _room(tmp_path)
    packed = room.pack(r, tmp_path / "out" / "blue-heron-42.room.tgz")
    assert packed["tables"]["church/ledger/hope/ledger.sqlite3"] == {"events": 3}
    assert packed["tables"]["box/doorkeeper.db"] == {"credentials": 1}
    into = tmp_path / "house-b" / "blue-heron-42"
    restored = room.restore(tmp_path / "out" / "blue-heron-42.room.tgz", into)
    assert restored["tables"] == packed["tables"] and restored["files"] == packed["files"]
    rows = sqlite3.connect(into / "church" / "ledger" / "hope" / "ledger.sqlite3").execute(
        "SELECT count(*) FROM events"
    )
    assert rows.fetchone()[0] == 3
    assert (into / "church" / "box.json").read_text() == '{"slug": "hope"}'


def test_a_restore_refuses_a_changed_pack_and_leaves_nothing(tmp_path):
    r = _room(tmp_path)
    out = tmp_path / "p.room.tgz"
    room.pack(r, out)
    with tarfile.open(out) as t:
        members = {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
    members["church/box.json"] = b'{"slug": "someone-else"}'
    bad = tmp_path / "bad.room.tgz"
    with tarfile.open(bad, "w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            import io

            t.addfile(info, io.BytesIO(data))
    into = tmp_path / "house-b" / "room"
    with pytest.raises(room.PackError, match="church/box.json"):
        room.restore(bad, into)
    assert not into.exists()


def test_a_restore_never_lands_on_a_room_that_exists(tmp_path):
    r = _room(tmp_path)
    out = tmp_path / "p.room.tgz"
    room.pack(r, out)
    with pytest.raises(room.PackError, match="already"):
        room.restore(out, r)


def test_a_pack_refuses_a_path_that_climbs_out(tmp_path):
    bad = tmp_path / "climb.room.tgz"
    import io

    with tarfile.open(bad, "w:gz") as t:
        data = json.dumps({"files": {"../x": "0"}, "tables": {}}).encode()
        info = tarfile.TarInfo(room.MANIFEST)
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo("../x")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(room.PackError):
        room.restore(bad, tmp_path / "into")
    assert not (tmp_path / "x").exists()


def test_the_cli_packs_and_restores_and_says_the_counts(tmp_path, capsys):
    from core.engine.cli import main

    r = _room(tmp_path)
    assert main(["room", "pack", str(r), "--out", str(tmp_path / "r.tgz")]) == 0
    said = json.loads(capsys.readouterr().out)
    assert said["tables"]["church/ledger/hope/ledger.sqlite3"] == {"events": 3}
    assert main(["room", "restore", str(tmp_path / "r.tgz"), "--into", str(r)]) == 1
    assert "already" in capsys.readouterr().err
