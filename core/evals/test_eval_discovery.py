"""Meta-eval: prove the auto-discovery convention works.

If this fails, the eval harness is no longer finding agent eval suites, which
would silently shrink the regression net.
"""

from __future__ import annotations

from core.evals import harness


def test_demo_agent_eval_suite_is_discovered():
    dirs = [d.parent.name for d in harness.agent_eval_dirs()]
    assert "demo" in dirs


def test_every_agent_with_jobs_ships_an_eval_suite():
    eval_agents = {d.parent.name for d in harness.agent_eval_dirs()}
    for agent in harness.agents_with_jobs():
        assert agent in eval_agents, f"agent {agent!r} has no evals/ directory"


def test_seed_and_agent_eval_files_are_found():
    files = {p.name for p in harness.discover_eval_files()}
    assert "test_demo_ingest.py" in files
