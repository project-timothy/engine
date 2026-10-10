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
from core.ledger.ledger import Ledger, LedgerRestoreError


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


# ---- the guard (follow-up to #421): an empty restore is refused, never rebuilt ----


def _pushed_remote(tmp_path: Path) -> Path:
    live = tmp_path / "live"
    assert main(["run", "demo", "demo", "ingest", "--ledger-dir", str(live)]) == 0
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/master")  # the host default that bit us
    _git(live / "demo", "push", "-q", str(remote), "main")
    return remote


def test_a_plain_clone_that_checked_out_nothing_is_refused(tmp_path):
    """The silent failure #421 found: the clone has history on origin/main
    but no files, and opening it used to build a fresh, empty ledger."""
    remote = _pushed_remote(tmp_path)
    restored = tmp_path / "new-box" / "demo"
    restored.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(remote), str(restored)], capture_output=True)
    assert not (restored / "ledger.sqlite3").exists(), "the precondition: nothing checked out"

    try:
        Ledger.open(restored)
    except LedgerRestoreError as exc:
        assert "clone -b main" in str(exc) and "docs/recovery.md" in str(exc)
    else:
        raise AssertionError("an empty checkout of a ledger with history opened")
    assert not (restored / "ledger.sqlite3").exists(), "the refusal builds nothing"


def test_a_job_against_an_empty_restore_fails_loudly(tmp_path, capsys):
    remote = _pushed_remote(tmp_path)
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    subprocess.run(["git", "clone", "-q", str(remote), "demo"], cwd=rebuilt, capture_output=True)

    code = main(["run", "demo", "demo", "ingest", "--ledger-dir", str(rebuilt)])
    assert code == 2
    assert "clone -b main" in capsys.readouterr().err
    assert not (rebuilt / "demo" / "ledger.sqlite3").exists()


def test_a_ledger_whose_database_went_missing_is_refused(tmp_path):
    """Commits say a ledger lived here; a missing database is a loss to
    restore, not a first run."""
    live = tmp_path / "live"
    assert main(["run", "demo", "demo", "ingest", "--ledger-dir", str(live)]) == 0
    (live / "demo" / "ledger.sqlite3").unlink()
    try:
        Ledger.open(live / "demo")
    except LedgerRestoreError:
        pass
    else:
        raise AssertionError("a ledger with commits and no database opened as new")


def test_a_fresh_install_with_its_backup_remote_already_named_still_opens(tmp_path):
    """Install order: the remote may be added before the first run. An
    unfetched remote has no branches yet, so this is still a first run."""
    root = tmp_path / "ledger" / "demo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", str(tmp_path / "remote.git"))
    with Ledger.open(root) as ledger:
        (runs,) = ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert runs == 0
