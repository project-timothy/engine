"""Git-commit wrapper for the ledger.

Git is the ledger's audit trail, recovery mechanism, and sync transport
(architecture 3.1). Every job commits its writes with a structured message
``agent/job-id: summary``. A re-run that produced no new rows leaves the tree
clean, so ``commit`` is a no-op in that case and returns ``None`` rather than
raising. That is what makes the whole "re-running a job is a no-op" guarantee
hold all the way down to the commit.

The committer identity is a fixed, neutral engine identity. It deliberately
names no tenant, so the bleed-through lint stays green and commits are
reproducible on any host (including CI, where no global git identity exists).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ENGINE_COMMITTER_NAME = "back-office-engine"
ENGINE_COMMITTER_EMAIL = "engine@localhost"


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_repo(root: Path) -> None:
    """Initialise a git repo at ``root`` if one is not already present."""
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").exists():
        return
    init = _run_git(root, "init", "-q")
    if init.returncode != 0:
        raise RuntimeError(f"git init failed at {root}: {init.stderr.strip()}")
    # The branch is the engine's, not the host's. `git init` reads
    # init.defaultBranch from whoever's config is in scope, so a ledger born on
    # a box that never set it came up on `master` while `ledger-backup.sh`
    # pushes `origin main`: "src refspec main does not match any", every night,
    # on every fresh install (found in the container, row 7.21, 2026-09-16).
    # symbolic-ref rather than `git init -b` so no git version is required.
    _run_git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    # Local identity so commits work even where no global identity is set.
    _run_git(root, "config", "user.name", ENGINE_COMMITTER_NAME)
    _run_git(root, "config", "user.email", ENGINE_COMMITTER_EMAIL)


def commit_all(root: Path, message: str) -> str | None:
    """Stage and commit everything under ``root``.

    Returns the new commit SHA, or ``None`` when there was nothing to commit
    (the idempotent re-run case).
    """
    added = _run_git(root, "add", "-A")
    if added.returncode != 0:
        raise RuntimeError(f"git add failed at {root}: {added.stderr.strip()}")
    staged = _run_git(root, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        # Exit 0 from --quiet means no staged differences: nothing to commit.
        return None
    committed = _run_git(root, "commit", "-q", "-m", message)
    if committed.returncode != 0:
        raise RuntimeError(f"git commit failed at {root}: {committed.stderr.strip()}")
    head = _run_git(root, "rev-parse", "HEAD")
    return head.stdout.strip() or None


def structured_message(agent: str, job: str, idempotency_key: str, summary: str) -> str:
    """Build the canonical ``agent/job-id: summary`` commit message."""
    short_key = idempotency_key.split(".")[-1][:12]
    first_line = f"{agent}/{job} [{short_key}]: {summary}".strip()
    return first_line
