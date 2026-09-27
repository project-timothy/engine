"""scripts/run-preflight.sh: the scheduled-run freshness guard (issue #108).

The guard runs before any engine/auditor code, in bash + git alone (running
project code to check project freshness would execute the possibly-stale code
it exists to catch). Contract:

- refuses (exit 1 + refusal marker) when the runtime checkout is on a branch
  other than main, has uncommitted changes, or cannot fast-forward to
  origin/main;
- pulls when cleanly behind, so merged fixes deploy on the next scheduled run;
- degrades (exit 0 + warning marker) when origin is unreachable — an offline
  night runs the existing reviewed main rather than skipping the audit;
- markers land under ``$AUDITOR_STORE_ROOT/preflight/`` where the heartbeat
  lens reports them (auditor/evals/test_preflight_markers.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-preflight.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",  # never read the real user's gitconfig
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """A bare 'origin' with one commit on main, and a 'runtime' clone carrying
    the preflight script at scripts/run-preflight.sh (the script locates its
    repo from its own path, mirroring the deploy layout)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main", ".")
    (seed / "README.md").write_text("engine\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")

    runtime = tmp_path / "runtime"
    _git(tmp_path, "clone", "-b", "main", str(origin), str(runtime))
    scripts_dir = runtime / "scripts"
    scripts_dir.mkdir()
    shutil.copy(SCRIPT, scripts_dir / "run-preflight.sh")
    (scripts_dir / "run-preflight.sh").chmod(0o755)
    # The script is tracked in the real repo; an untracked copy would (rightly)
    # trip the guard's own dirty-tree refusal.
    _git(runtime, "add", "scripts")
    _git(runtime, "commit", "-m", "track preflight script")
    _git(runtime, "push", "origin", "main")
    _git(seed, "pull", "--ff-only", "origin", "main")

    store = tmp_path / "store"
    return origin, seed, runtime, store


def _preflight(runtime: Path, store: Path, job: str = "auditor-nightly", **env: str):
    return subprocess.run(
        ["bash", str(runtime / "scripts" / "run-preflight.sh"), job],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "AUDITOR_STORE_ROOT": str(store),
            **GIT_ENV,
            **env,
        },
    )


def _markers(store: Path) -> list[dict]:
    d = store / "preflight"
    if not d.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


def _advance_origin(seed: Path, name: str = "next") -> str:
    (seed / f"{name}.txt").write_text(name)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", name)
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def test_clean_current_main_passes_without_markers(repos):
    _, _, runtime, store = repos
    result = _preflight(runtime, store)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _markers(store) == []


def test_behind_origin_pulls_to_current(repos):
    _, seed, runtime, store = repos
    new_head = _advance_origin(seed)
    result = _preflight(runtime, store)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(runtime, "rev-parse", "HEAD") == new_head
    assert _markers(store) == []


def test_feature_branch_refuses(repos):
    _, _, runtime, store = repos
    _git(runtime, "checkout", "-b", "fix/something")
    result = _preflight(runtime, store)
    assert result.returncode != 0
    markers = _markers(store)
    assert len(markers) == 1
    assert markers[0]["kind"] == "refusal"
    assert markers[0]["reason"] == "branch-not-main"
    assert markers[0]["job"] == "auditor-nightly"


def test_dirty_tree_refuses(repos):
    _, _, runtime, store = repos
    (runtime / "README.md").write_text("edited mid-session\n")
    result = _preflight(runtime, store)
    assert result.returncode != 0
    markers = _markers(store)
    assert len(markers) == 1
    assert markers[0]["reason"] == "dirty-tree"


def test_diverged_local_main_refuses_without_rewriting(repos):
    _, seed, runtime, store = repos
    (runtime / "local.txt").write_text("local-only commit\n")
    _git(runtime, "add", ".")
    _git(runtime, "commit", "-m", "local divergence")
    local_head = _git(runtime, "rev-parse", "HEAD")
    _advance_origin(seed)
    result = _preflight(runtime, store)
    assert result.returncode != 0
    markers = _markers(store)
    assert len(markers) == 1
    assert markers[0]["reason"] == "diverged"
    # The guard never rewrites history to force freshness.
    assert _git(runtime, "rev-parse", "HEAD") == local_head


def test_unreachable_origin_degrades_but_runs(repos):
    origin, _, runtime, store = repos
    shutil.rmtree(origin)
    head_before = _git(runtime, "rev-parse", "HEAD")
    result = _preflight(runtime, store, job="engine-ap-daily")
    assert result.returncode == 0, result.stdout + result.stderr
    markers = _markers(store)
    assert len(markers) == 1
    assert markers[0]["kind"] == "warning"
    assert markers[0]["reason"] == "fetch-failed"
    assert markers[0]["job"] == "engine-ap-daily"
    assert _git(runtime, "rev-parse", "HEAD") == head_before


def test_marker_json_carries_the_forensics(repos):
    _, _, runtime, store = repos
    _git(runtime, "checkout", "-b", "fix/whatever")
    _preflight(runtime, store)
    (marker,) = _markers(store)
    for key in ("job", "kind", "reason", "detail", "ts", "branch", "head"):
        assert key in marker, f"marker missing {key}"
    assert marker["branch"] == "fix/whatever"


# ---- the container (phase 7 row 7.21) ----------------------------------------
#
# A container image is a different kind of reviewed checkout: the image digest
# IS the review, there is no origin to fetch from, and pulling code into a
# running container is exactly what an image deploy replaces. The image says so
# by setting ENGINE_IMAGE; the guard then reports what it is running and exits
# 0. That excuse applies ONLY to the absence of a repository: on a checkout the
# normal path runs, whatever the environment claims.


def _imageless_runtime(tmp_path: Path) -> Path:
    """The container's layout: the scripts at scripts/, and no repository
    metadata anywhere (the image is a COPY of the tree, not a clone)."""
    root = tmp_path / "image-app"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "run-preflight.sh")
    (root / "scripts" / "run-preflight.sh").chmod(0o755)
    return root


def test_an_image_that_declares_itself_passes_without_a_checkout(tmp_path):
    store = tmp_path / "store"
    result = _preflight(
        _imageless_runtime(tmp_path),
        store,
        job="engine-ap-daily",
        ENGINE_IMAGE="engine:sha-abc123",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "engine:sha-abc123" in result.stdout


def test_the_image_path_writes_no_marker(tmp_path):
    """Every marker that is not a refusal becomes a nightly WARN finding in
    the heartbeat lens. A container that wrote one at 02:00, 08:00 and 23:00
    would nag forever about being a container, and the lens would be muted."""
    store = tmp_path / "store"
    _preflight(_imageless_runtime(tmp_path), store, ENGINE_IMAGE="engine:sha-abc123")
    assert _markers(store) == []


def test_without_the_declaration_a_tree_with_no_repository_still_refuses(tmp_path):
    store = tmp_path / "store"
    result = _preflight(_imageless_runtime(tmp_path), store)
    assert result.returncode != 0
    (marker,) = _markers(store)
    assert marker["kind"] == "refusal"
    assert marker["reason"] == "git-broken"


def test_a_real_checkout_ignores_the_image_declaration(repos):
    """The one that matters for this Mac: ENGINE_IMAGE may not switch the
    freshness guard off where there is a checkout to check."""
    _, _, runtime, store = repos
    (runtime / "scratch.txt").write_text("uncommitted\n")
    result = _preflight(runtime, store, ENGINE_IMAGE="engine:sha-abc123")
    assert result.returncode != 0
    (marker,) = _markers(store)
    assert marker["reason"] == "dirty-tree"


# ---- the tenant's own checkout (2026-09-22) ------------------------------------
#
# docs/decisions/2026-09-22-the-tenant-lives-in-its-own-repository.md: the
# schedule names the tenant checkout as ENGINE_TENANTS_ROOT (<clone>/tenants),
# and a mute merged there must deploy the way a fix merged here does, so the
# guard applies the same rules to it, with a "tenant-" reason so the heartbeat
# lens can tell the two apart.


def _tenant_pair(tmp_path: Path):
    """A second bare origin + clone, shaped like the tenant repository:
    tenants/<slug>/tenant.toml on main."""
    origin = tmp_path / "tenant-origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")
    seed = tmp_path / "tenant-seed"
    (seed / "tenants" / "acme").mkdir(parents=True)
    _git(seed, "init", "-b", "main", ".")
    (seed / "tenants" / "acme" / "tenant.toml").write_text('[identity]\nslug = "acme"\n')
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "tenant seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    clone = tmp_path / "tenant-runtime"
    _git(tmp_path, "clone", "-b", "main", str(origin), str(clone))
    return seed, clone


def test_a_tenant_checkout_named_by_engine_tenants_root_is_freshened_too(repos, tmp_path):
    _, _, runtime, store = repos
    seed, clone = _tenant_pair(tmp_path)
    (seed / "tenants" / "acme" / "triage.toml").write_text("# a mute merged upstream\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "mute")
    _git(seed, "push", "origin", "main")
    new_head = _git(seed, "rev-parse", "HEAD")
    result = _preflight(runtime, store, ENGINE_TENANTS_ROOT=str(clone / "tenants"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(clone, "rev-parse", "HEAD") == new_head
    assert (clone / "tenants" / "acme" / "triage.toml").exists()
    assert _markers(store) == []
    assert "tenant-HEAD=" in result.stdout


def test_a_dirty_tenant_checkout_refuses_with_its_own_reason(repos, tmp_path):
    _, _, runtime, store = repos
    _, clone = _tenant_pair(tmp_path)
    (clone / "tenants" / "acme" / "tenant.toml").write_text("edited by hand\n")
    result = _preflight(runtime, store, ENGINE_TENANTS_ROOT=str(clone / "tenants"))
    assert result.returncode != 0
    (marker,) = _markers(store)
    assert marker["kind"] == "refusal"
    assert marker["reason"] == "tenant-dirty-tree"
    assert str(clone) in marker["detail"]
    assert marker["branch"] == "main"


def test_a_tenants_root_outside_any_checkout_is_left_alone(repos, tmp_path):
    """An image or a plain folder: nothing to freshen, nothing to refuse."""
    _, _, runtime, store = repos
    plain = tmp_path / "plain" / "tenants"
    plain.mkdir(parents=True)
    result = _preflight(runtime, store, ENGINE_TENANTS_ROOT=str(plain))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _markers(store) == []


def test_the_engine_is_guarded_before_the_tenant(repos, tmp_path):
    """A refused engine never reaches the tenant: one refusal, the engine's."""
    _, _, runtime, store = repos
    _, clone = _tenant_pair(tmp_path)
    (runtime / "README.md").write_text("edited mid-session\n")
    (clone / "tenants" / "acme" / "tenant.toml").write_text("also edited\n")
    result = _preflight(runtime, store, ENGINE_TENANTS_ROOT=str(clone / "tenants"))
    assert result.returncode != 0
    (marker,) = _markers(store)
    assert marker["reason"] == "dirty-tree"
