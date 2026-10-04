"""Unit tests for the Ledger facade (engine invariants 1 and 3)."""

from __future__ import annotations

import json
import subprocess

from core.ledger import Ledger
from core.ledger.git_commit import ensure_repo


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.strip()


def test_open_initialises_git_repo_and_schema(tmp_path):
    root = tmp_path / "deploy"
    with Ledger.open(root) as ledger:
        assert (root / ".git").is_dir()
        assert ledger.db_path.exists()
        from core.ledger.schema import MIGRATIONS

        versions = ledger._conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert sorted(v[0] for v in versions) == sorted(m[0] for m in MIGRATIONS)
        tables = {
            r[0]
            for r in ledger._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"runs", "events", "approval_queue", "schema_migrations"} <= tables


def test_record_run_is_idempotent_on_key(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        first = ledger.record_run(
            idempotency_key="k1",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json='{"status":"ok"}',
            summary="did a thing",
        )
        assert first.is_new is True

        second = ledger.record_run(
            idempotency_key="k1",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json='{"status":"ok"}',
            summary="did a thing",
        )
        assert second.is_new is False
        assert second.id == first.id

        count = ledger._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert count == 1


def test_find_run_returns_stored_result(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        ledger.record_run(
            idempotency_key="k2",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=True,
            result_json='{"answer":42}',
            summary="s",
        )
        found = ledger.find_run("k2")
        assert found is not None
        assert found.shadow is True
        assert json.loads(found.result_json)["answer"] == 42
        assert ledger.find_run("missing") is None


def test_append_event_dedupes_and_writes_jsonl(tmp_path):
    root = tmp_path / "d"
    with Ledger.open(root) as ledger:
        run = ledger.record_run(
            idempotency_key="r",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="",
        )
        first = ledger.append_event(
            idempotency_key="e1",
            run_id=run.id,
            tenant="t",
            agent="demo",
            event_type="fixture.read",
            payload={"n": 1},
        )
        dup = ledger.append_event(
            idempotency_key="e1",
            run_id=run.id,
            tenant="t",
            agent="demo",
            event_type="fixture.read",
            payload={"n": 1},
        )
        assert first is True
        assert dup is False

        # Exactly one row, exactly one JSONL line.
        assert ledger._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        log = ledger.read_event_log()
        assert len(log) == 1
        assert log[0]["event_type"] == "fixture.read"
        assert log[0]["payload"] == {"n": 1}


def test_enqueue_approval_is_idempotent(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        run = ledger.record_run(
            idempotency_key="r",
            tenant="t",
            agent="demo",
            job="ingest",
            status="needs_approval",
            shadow=False,
            result_json="{}",
            summary="",
        )
        a = ledger.enqueue_approval(
            idempotency_key="ap1",
            run_id=run.id,
            tenant="t",
            agent="demo",
            action_type="external.send",
            params={"to": "x"},
        )
        b = ledger.enqueue_approval(
            idempotency_key="ap1",
            run_id=run.id,
            tenant="t",
            agent="demo",
            action_type="external.send",
            params={"to": "x"},
        )
        assert a is True
        assert b is False
        pending = ledger.pending_approvals("t")
        assert len(pending) == 1
        assert pending[0]["action_type"] == "external.send"


def test_commit_then_noop_returns_none(tmp_path):
    root = tmp_path / "d"
    with Ledger.open(root) as ledger:
        ledger.record_run(
            idempotency_key="r",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="first",
        )
        sha = ledger.commit(agent="demo", job="ingest", idempotency_key="r", summary="first run")
        assert sha is not None
        # Nothing changed: a second commit is a no-op.
        again = ledger.commit(agent="demo", job="ingest", idempotency_key="r", summary="first run")
        assert again is None

        msg = _git(root, "log", "-1", "--pretty=%s")
        assert msg.startswith("demo/ingest [")
        assert "first run" in msg


def test_a_new_ledger_is_born_on_main_whatever_the_host_prefers(tmp_path, monkeypatch):
    """Found in the container (row 7.21, 2026-09-16): `git init` takes its
    branch name from the HOST's config, so the ledger came up on master and
    the 23:00 `git push origin main` failed with "src refspec main does not
    match any" on every fresh install. The branch the backup pushes is part
    of the engine's contract, not a property of whoever ran the installer."""
    config = tmp_path / "gitconfig"
    config.write_text("[init]\n\tdefaultBranch = master\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "ledger"
    ensure_repo(root)
    head = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip() == "main", head.stdout + head.stderr
