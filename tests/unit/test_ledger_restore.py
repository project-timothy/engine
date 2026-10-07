"""The first recovery tier works: a ledger comes back from its remote (public #13).

The 23:00 job pushes the ledger repository to a remote. Until this test,
nothing proved the other half: that a clone of that remote, on a box that has
never seen the ledger, opens as a ledger whose event log and tables agree.
docs/recovery.md is the operator's side of the same drill.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.engine.cli import main
from core.ledger.event_log import event_log_is_whole, read_event_lines
from core.ledger.ledger import Ledger


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _tables(ledger: Ledger) -> dict[str, list[tuple]]:
    conn = ledger.conn
    return {
        "runs": [tuple(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")],
        "events": [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")],
        "approval_queue": [
            tuple(r) for r in conn.execute("SELECT * FROM approval_queue ORDER BY id")
        ],
    }


def test_a_ledger_restored_from_its_remote_matches_the_one_that_pushed(tmp_path):
    live = tmp_path / "live"
    assert main(["run", "demo", "demo", "ingest", "--ledger-dir", str(live)]) == 0
    source = live / "demo"

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-q", "origin", "main")  # what ledger-backup.sh does at 23:00

    restored = tmp_path / "new-box" / "demo"
    restored.parent.mkdir()
    # -b main: a bare remote made by `git init --bare` on a host with no
    # init.defaultBranch points HEAD at master, and a plain clone of it checks
    # out NOTHING. Ledger.open on that empty directory then builds a fresh,
    # empty ledger without a word (found writing this test, 2026-10-06).
    _git(restored.parent, "clone", "-q", "-b", "main", str(remote), str(restored))

    with Ledger.open(source) as before, Ledger.open(restored) as after:
        assert _tables(after) == _tables(before)
        (events,) = after.conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert events > 0, "the drill restores a ledger with something in it"
        assert event_log_is_whole(restored, events)
        assert read_event_lines(restored) == after._event_rows()
    assert _git(restored, "status", "--porcelain") == "", "opening a restore changes nothing"


def test_a_restored_ledger_runs_the_next_job_as_a_rerun(tmp_path, capsys):
    """Idempotency survives the restore: the same job against the clone is a
    noop, so a rebuilt box does not post yesterday's work twice."""
    live = tmp_path / "live"
    assert main(["run", "demo", "demo", "ingest", "--ledger-dir", str(live)]) == 0
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(live / "demo", "push", "-q", str(remote), "main")
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    _git(rebuilt, "clone", "-q", "-b", "main", str(remote), "demo")

    capsys.readouterr()
    assert main(["run", "demo", "demo", "ingest", "--ledger-dir", str(rebuilt)]) == 0
    with Ledger.open(rebuilt / "demo") as ledger:
        (runs,) = ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert runs == 1
    assert "noop" in capsys.readouterr().out
