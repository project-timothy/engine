"""Agent eval for demo/ingest.

Auto-discovered by the eval harness because it lives under an agent's
``evals/`` directory. Exercises the agent through the real runner so the eval
covers the whole pipe (config -> job -> ledger -> result), not just the job
function in isolation.

This file is under ``core/``, so it names no tenant: it loads the expected
legal name from config rather than hardcoding one (the bleed-through lint
would reject a literal business name here, which is the point).
"""

from __future__ import annotations

from core.engine.config import load_tenant
from core.engine.runner import run


def test_demo_ingest_records_one_event_per_item(tmp_path):
    result = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert result.status == "ok"
    assert len(result.actions) == 3
    assert result.approvals_needed == []  # demo moves no money, sends nothing


def test_demo_ingest_summary_is_config_driven(tmp_path):
    # Proves tenant config flowed into the job: the legal name in the summary
    # is whatever the tenant config says, not a value baked into core/.
    expected = load_tenant("demo").identity.legal_name
    result = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert expected in result.summary
