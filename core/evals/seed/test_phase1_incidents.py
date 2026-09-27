"""Seed evals that test Phase 1 code directly. These run and must pass.

Each maps an incident to a guarantee the foundation already makes, so the
regression net has teeth from day one rather than only after Phase 2.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

from core.engine.runner import resolve_ledger_root, run

# core/evals/seed/test_phase1_incidents.py -> parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _commit_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def test_idempotent_rerun_is_a_noop(tmp_path):
    """Invariant 3: every job is safe to re-run; re-running never double-posts."""
    first = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert first.status == "ok"
    root = resolve_ledger_root("demo", tmp_path)
    commits = _commit_count(root)

    second = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert second.status == "noop"
    assert second.commit is None
    assert _commit_count(root) == commits  # no new commit on re-run


def test_ledger_is_single_home_no_duplicate_writes(tmp_path):
    """2026-04-23 / 2026-05-06: one home per record, no duplicate copies.

    The engine form: an event is written exactly once. A second run does not
    append a second copy to the append-only log.
    """
    run("demo", "demo", "ingest", ledger_dir=tmp_path)
    run("demo", "demo", "ingest", ledger_dir=tmp_path)  # re-run
    log_path = resolve_ledger_root("demo", tmp_path) / "event_log.jsonl"
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3  # three items, written once, not six


def test_dependencies_are_pinned():
    """2026-05-14: unpinned dependency drift silently breaks working pipelines.

    Every declared dependency, runtime and dev, must pin an exact version.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    runtime = data["project"]["dependencies"]
    dev = data.get("dependency-groups", {}).get("dev", [])
    for spec in [*runtime, *dev]:
        assert "==" in spec, f"dependency {spec!r} is not pinned to an exact version"
    # And the lockfile exists (the deterministic resolution, committed as code).
    assert (REPO_ROOT / "uv.lock").exists()


def test_secrets_never_committed_to_the_repo():
    """2026-05-21 secret-scan rule + the secrets-as-references invariant.

    No secret-bearing files are tracked, and tenant configs reference secrets
    by environment-variable name rather than carrying values.
    """
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    banned_suffixes = (".env", ".pem", ".key")
    banned_names = {"credentials.json"}
    for path in tracked:
        name = Path(path).name
        assert not name.endswith(banned_suffixes), f"secret-bearing file tracked: {path}"
        assert name not in banned_names, f"secret-bearing file tracked: {path}"

    # Tenant secret references must be env-var names (UPPER_SNAKE), not values.
    for tenant_toml in (REPO_ROOT / "tenants").glob("*/tenant.toml"):
        data = tomllib.loads(tenant_toml.read_text())
        for logical, ref in data.get("secrets", {}).items():
            assert ref == ref.upper(), f"{tenant_toml}: secret {logical} is not an env-var ref"


def test_all_seed_fixtures_are_valid_json():
    """The skipped Phase 2 evals are only useful if their fixtures are in place
    and parse now. This guards that."""
    fixtures = sorted(SEED_FIXTURES.glob("*.json"))
    assert fixtures, "no seed fixtures found"
    for path in fixtures:
        json.loads(path.read_text())  # raises on malformed fixture
