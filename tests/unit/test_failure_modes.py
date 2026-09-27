"""Unit tests for run-failure semantics (the "failure modes" rubric point).

A job that raises must surface as a structured ``RunResult`` with
``status="error"`` and an anomaly, never as a raw traceback. A failed run is
NOT recorded in the ledger and NOT committed, so re-running after the cause is
fixed executes the job instead of replaying the failure.
"""

from __future__ import annotations

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger


def test_job_exception_returns_structured_error(tmp_path):
    # Point the demo agent at a fixture that does not exist; loading it raises.
    result = run(
        "demo",
        "demo",
        "ingest",
        params={"fixture": str(tmp_path / "missing.json")},
        ledger_dir=tmp_path,
    )
    assert result.status == "error"
    assert result.commit is None
    assert result.anomalies, "an error run must carry an anomaly describing the failure"
    assert result.anomalies[0].code == "job.exception"
    assert "missing.json" in result.anomalies[0].detail


def test_failed_run_is_not_recorded_so_fixed_rerun_executes(tmp_path):
    bad = run(
        "demo",
        "demo",
        "ingest",
        params={"fixture": str(tmp_path / "missing.json")},
        ledger_dir=tmp_path,
    )
    assert bad.status == "error"

    # Nothing was recorded: no run row, no commit on the ledger repo.
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        assert ledger._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    # The same command with the cause fixed executes normally (no error replay).
    good = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert good.status == "ok"
    assert good.commit is not None
