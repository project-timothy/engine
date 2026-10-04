"""Unit tests for the run framework: end-to-end pipe + idempotency."""

from __future__ import annotations

import subprocess

from core.engine.config import default_tenants_root
from core.engine.runner import resolve_ledger_root, run


def _commit_count(root):
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def test_demo_run_writes_ledger_and_commits(tmp_path):
    result = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert result.status == "ok"
    assert result.agent == "demo"
    assert result.tenant == "demo"
    assert len(result.actions) == 3
    assert result.commit is not None
    # Rubric is the contract stub in Phase 1.
    assert result.rubric.is_stub

    root = resolve_ledger_root("demo", tmp_path)
    assert _commit_count(root) == 1


def test_rerun_is_a_verified_noop(tmp_path):
    first = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert first.status == "ok"
    root = resolve_ledger_root("demo", tmp_path)
    commits_after_first = _commit_count(root)

    second = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert second.status == "noop"
    assert second.commit is None
    # No new commit, exactly one run row, three event rows (not six).
    assert _commit_count(root) == commits_after_first
    from core.ledger import Ledger

    with Ledger.open(root) as ledger:
        runs = ledger._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        events = ledger._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert runs == 1
        assert events == 3


def test_shadow_flag_is_recorded(tmp_path):
    result = run("demo", "demo", "ingest", shadow=True, ledger_dir=tmp_path)
    assert result.shadow is True


def test_two_tenants_have_isolated_ledgers(tmp_path):
    """Two tenants side by side, the demo and a copy of it under another
    slug, write two ledgers."""
    tenants = tmp_path / "tenants"
    for slug in ("demo", "other"):
        (tenants / slug).mkdir(parents=True)
        text = (default_tenants_root() / "demo" / "tenant.toml").read_text()
        (tenants / slug / "tenant.toml").write_text(
            text.replace('slug = "demo"', f'slug = "{slug}"')
        )
    ledgers = tmp_path / "ledgers"
    run("other", "demo", "ingest", ledger_dir=ledgers, tenants_root=tenants)
    run("demo", "demo", "ingest", ledger_dir=ledgers, tenants_root=tenants)
    assert resolve_ledger_root("other", ledgers) != resolve_ledger_root("demo", ledgers)
    assert (resolve_ledger_root("other", ledgers) / "ledger.sqlite3").exists()
    assert (resolve_ledger_root("demo", ledgers) / "ledger.sqlite3").exists()
