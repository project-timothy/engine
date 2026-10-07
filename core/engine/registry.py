"""Agent and job discovery.

Agents live under ``core/agents/<name>/`` with a ``jobs.py`` exposing
``JOBS: dict[str, JobHandler]``. The registry resolves an agent name and job
name to a handler, with explicit errors when either is unknown.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contracts import JobHandler

AGENTS_PACKAGE = "core.agents"

# An approval check: ``(ledger, tenant, merged_params) -> refusal message or
# None``. An agent's ``jobs.py`` may expose ``APPROVAL_CHECKS: dict[action_type,
# check]`` for cards whose approval must carry a fact the owner supplies
# (phase 7 row 7.1: ``ap.reconcile_review`` needs ``--param row=<id>``). The
# queue runs the check on the merged params BEFORE recording the decision, so
# a refused approval leaves the card pending instead of approved-but-stuck.
ApprovalCheck = Callable[[Any, str, dict[str, Any]], str | None]


class UnknownAgentError(LookupError):
    pass


class UnknownJobError(LookupError):
    pass


def agents_dir() -> Path:
    # core/engine/registry.py -> parents[1] is core/, then agents/.
    return Path(__file__).resolve().parents[1] / "agents"


def agent_dir(agent: str) -> Path:
    return agents_dir() / agent


def list_agents() -> list[str]:
    base = agents_dir()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "jobs.py").exists())


def load_agent_jobs(agent: str) -> dict[str, JobHandler]:
    if not (agent_dir(agent) / "jobs.py").exists():
        raise UnknownAgentError(
            f"no agent {agent!r}; known agents: {', '.join(list_agents()) or 'none'}"
        )
    module = importlib.import_module(f"{AGENTS_PACKAGE}.{agent}.jobs")
    jobs = getattr(module, "JOBS", None)
    if not isinstance(jobs, dict):
        raise UnknownAgentError(f"agent {agent!r} jobs.py does not expose a JOBS dict")
    return jobs


def load_approval_checks(agent: str) -> dict[str, ApprovalCheck]:
    """The agent's approval-time checks by action type; empty when the agent
    declares none (every card approves as before)."""
    if not (agent_dir(agent) / "jobs.py").exists():
        return {}
    module = importlib.import_module(f"{AGENTS_PACKAGE}.{agent}.jobs")
    checks = getattr(module, "APPROVAL_CHECKS", None)
    return dict(checks) if isinstance(checks, dict) else {}


def load_human_only(agent: str) -> frozenset[str]:
    """The agent's human-only card types (``HUMAN_ONLY`` in its jobs.py): cards
    the queue refuses to resolve without a person at a terminal (#356)."""
    if not (agent_dir(agent) / "jobs.py").exists():
        return frozenset()
    module = importlib.import_module(f"{AGENTS_PACKAGE}.{agent}.jobs")
    declared = getattr(module, "HUMAN_ONLY", None)
    return frozenset(declared) if isinstance(declared, set | frozenset) else frozenset()


def get_job(agent: str, job: str) -> JobHandler:
    jobs = load_agent_jobs(agent)
    if job not in jobs:
        raise UnknownJobError(
            f"agent {agent!r} has no job {job!r}; jobs: {', '.join(sorted(jobs)) or 'none'}"
        )
    handler = jobs[job]
    if not isinstance(handler, JobHandler):
        raise UnknownJobError(f"job {agent!r}/{job!r} is not a JobHandler")
    return handler
