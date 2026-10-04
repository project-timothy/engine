"""Agent eval for demo/flaky, the retry-policy fixture (phase 7 row 7.23).

The job exists so the cross-run retry has a deterministic witness: it fails
``fail_times`` times with the named ``cause`` (an exception carrying the
``cause`` / ``transient`` taxonomy the extractor and the gateway already
use), records one durable move on its first attempt, and succeeds after.
It is the only policy holder in the repo; no production job gains one in
this row.
"""

from __future__ import annotations

from core.engine.runner import run


def test_flaky_succeeds_at_once_when_told_not_to_fail(tmp_path):
    result = run("demo", "demo", "flaky", params={"fail_times": "0"}, ledger_dir=tmp_path)
    assert result.status == "ok"
    assert "attempt 1" in result.summary
    assert "recorded the move" in result.summary
    assert not any(a.code.startswith("engine.retry") for a in result.anomalies)


def test_flaky_fails_with_the_named_cause_by_default(tmp_path):
    result = run("demo", "demo", "flaky", ledger_dir=tmp_path)
    assert result.status == "error"
    (exc,) = [a for a in result.anomalies if a.code == "job.exception"]
    assert "transport_error" in exc.detail
    assert any(a.code == "engine.retry.scheduled" for a in result.anomalies)
