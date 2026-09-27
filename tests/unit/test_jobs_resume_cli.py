"""``engine jobs resume <tenant> [--now]`` (phase 7 row 7.23).

The command a scheduler runs every 15 minutes (row 7.21 wires the cadence;
nothing is scheduled on any host by this row). Nothing due: one line, exit
0. Otherwise it executes each due retry through the runner and reports what
it ran; ``--now`` forces every scheduled retry due (the owner's tool).
"""

from __future__ import annotations

from core.engine.cli import main
from core.engine.runner import RETRY_RECORD, resolve_ledger_root
from core.ledger import Ledger


def _fail_once(tmp_path):
    return main(
        [
            "run",
            "demo",
            "demo",
            "flaky",
            "--ledger-dir",
            str(tmp_path),
            "--param",
            "fail_times=1",
            "--param",
            "cause=transport_error",
        ]
    )


def _retries(tmp_path):
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        return ledger.job_records(tenant="demo", record_type=RETRY_RECORD)


def test_nothing_due_exits_zero_with_one_line(tmp_path, capsys):
    code = main(["jobs", "resume", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip().splitlines() == ["no retries due for demo"]


def test_a_retry_not_yet_due_is_left_alone_without_now(tmp_path, capsys):
    assert _fail_once(tmp_path) == 1
    capsys.readouterr()
    code = main(["jobs", "resume", "demo", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "no retries due" in out
    assert _retries(tmp_path)[0]["retry_state"] == "scheduled"


def test_now_forces_the_due_retry_and_reports_what_ran(tmp_path, capsys):
    assert _fail_once(tmp_path) == 1
    capsys.readouterr()
    code = main(["jobs", "resume", "demo", "--now", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "demo/flaky @ demo: ok" in out
    assert "attempt 2 of 3" in out
    r1, r2 = _retries(tmp_path)
    assert (r1["retry_state"], r2["retry_state"]) == ("resumed", "succeeded")
    # and again: nothing left
    code = main(["jobs", "resume", "demo", "--now", "--ledger-dir", str(tmp_path)])
    assert code == 0
    assert "no retries due" in capsys.readouterr().out


def test_a_retry_that_fails_again_exits_one_and_stays_scheduled(tmp_path, capsys):
    main(
        [
            "run",
            "demo",
            "demo",
            "flaky",
            "--ledger-dir",
            str(tmp_path),
            "--param",
            "fail_times=2",
            "--param",
            "cause=timeout",
        ]
    )
    capsys.readouterr()
    code = main(["jobs", "resume", "demo", "--now", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "demo/flaky @ demo: error" in out
    assert _retries(tmp_path)[-1]["retry_state"] == "scheduled"


def test_json_emits_the_results(tmp_path, capsys):
    import json

    assert _fail_once(tmp_path) == 1
    capsys.readouterr()
    code = main(["jobs", "resume", "demo", "--now", "--json", "--ledger-dir", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [r["status"] for r in payload] == ["ok"]


def test_unknown_tenant_exits_two(tmp_path, capsys):
    code = main(["jobs", "resume", "no-such-tenant", "--ledger-dir", str(tmp_path)])
    assert code == 2
    assert "no tenant config" in capsys.readouterr().err
