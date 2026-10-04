"""Eval harness: discovery of agent eval suites and shared fixture access.

The convention (architecture 3.4): each agent owns an ``evals/`` directory and
its cases are auto-discovered. pytest already collects ``test_*.py`` under
``core/`` (configured in pyproject ``testpaths``), so discovery is "free"; this
module exposes the same discovery programmatically so a meta-eval can assert
every agent ships an eval suite and so tooling can enumerate cases.
"""

from __future__ import annotations

from pathlib import Path


def core_root() -> Path:
    # core/evals/harness.py -> parents[1] is core/.
    return Path(__file__).resolve().parents[1]


def agents_root() -> Path:
    return core_root() / "agents"


def seed_dir() -> Path:
    return core_root() / "evals" / "seed"


def agent_eval_dirs() -> list[Path]:
    """Every ``core/agents/<name>/evals`` directory that exists."""
    base = agents_root()
    if not base.is_dir():
        return []
    return sorted(p / "evals" for p in base.iterdir() if p.is_dir() and (p / "evals").is_dir())


def agents_with_jobs() -> list[str]:
    base = agents_root()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "jobs.py").exists())


def discover_eval_files() -> list[Path]:
    """All eval test files: per-agent ``evals/`` plus the seed corpus."""
    files: list[Path] = []
    for d in agent_eval_dirs():
        files.extend(sorted(d.glob("test_*.py")))
    files.extend(sorted(seed_dir().glob("test_*.py")))
    return files
