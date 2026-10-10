"""Unit tests for the CLI entry point (previously only covered via smoke runs)."""

from __future__ import annotations

from core.engine.cli import main


def test_run_ok_exits_zero(tmp_path, capsys):
    code = main(["run", "demo", "demo", "ingest", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "demo/ingest @ demo: ok" in out


def test_rerun_noop_exits_zero(tmp_path, capsys):
    main(["run", "demo", "demo", "ingest", "--ledger-dir", str(tmp_path)])
    code = main(["run", "demo", "demo", "ingest", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "noop" in out


def test_job_error_exits_one_with_anomaly(tmp_path, capsys):
    code = main(
        [
            "run",
            "demo",
            "demo",
            "ingest",
            "--ledger-dir",
            str(tmp_path),
            "--param",
            f"fixture={tmp_path / 'missing.json'}",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "error" in out
    assert "anomalies: 1" in out


def test_unknown_agent_exits_two(tmp_path, capsys):
    code = main(["run", "demo", "no-such-agent", "ingest", "--ledger-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "no agent" in err


def test_unknown_tenant_exits_two(tmp_path, capsys):
    code = main(["run", "no-such-tenant", "demo", "ingest", "--ledger-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "no tenant config" in err


def test_bad_param_exits_two(tmp_path, capsys):
    code = main(["run", "demo", "demo", "ingest", "--param", "not-a-pair"])
    err = capsys.readouterr().err
    assert code == 2
    assert "K=V" in err


def test_agents_lists_demo(capsys):
    code = main(["agents"])
    out = capsys.readouterr().out
    assert code == 0
    # demo gained the retry fixture job in row 7.23; the intent is unchanged
    assert "demo: flaky, ingest" in out


def test_json_output_is_parseable(tmp_path, capsys):
    import json

    code = main(["run", "demo", "demo", "ingest", "--ledger-dir", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert parsed["status"] == "ok"
    assert parsed["rubric"]["notes"].startswith("stub")


def test_close_preflight_packet_error_exits_two(tmp_path, capsys, monkeypatch):
    """Honesty audit 2026-09-03, substrate finding F5: ``close-preflight
    --packet`` used to exit by the preflight verdict alone, so a packet run
    that FAILED (guard refusal, render exception) still exited 0. The packet
    leg's error now exits 2 with the packet summary on stderr."""
    from core.engine import runner as runner_mod
    from core.engine.contracts import JobHandler, JobOutput

    handlers = {
        "preflight": JobHandler(
            key=lambda ctx: "preflight",
            run=lambda ctx: JobOutput(status="ok", summary="close preflight: verdict OK"),
        ),
        "packet": JobHandler(
            key=lambda ctx: "packet",
            run=lambda ctx: JobOutput(status="error", summary="packet render failed: boom"),
        ),
    }
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: handlers[job])

    code = main(["close-preflight", "demo", "--packet", "--ledger-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "verdict OK" in captured.out
    assert "packet render failed: boom" in captured.err


def test_close_preflight_packet_ok_keeps_the_verdict_exit(tmp_path, capsys, monkeypatch):
    from core.engine import runner as runner_mod
    from core.engine.contracts import JobHandler, JobOutput

    handlers = {
        "preflight": JobHandler(
            key=lambda ctx: "preflight",
            run=lambda ctx: JobOutput(status="ok", summary="close preflight: verdict WARN"),
        ),
        "packet": JobHandler(
            key=lambda ctx: "packet",
            run=lambda ctx: JobOutput(status="ok", summary="packet rendered"),
        ),
    }
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: handlers[job])

    code = main(["close-preflight", "demo", "--packet", "--ledger-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "packet rendered" in out


def test_unknown_tenant_lists_the_known_ones_and_names_init(tmp_path, capsys):
    # Issue #6 (2026-10-06): the bare path left a newcomer with nowhere to go.
    code = main(["run", "nosuch", "demo", "ingest", "--ledger-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "known tenants: demo" in err
    assert "engine init nosuch" in err


def test_a_missing_landing_folder_names_engine_doctor(tmp_path, capsys, monkeypatch):
    # Issue #6: the summary was a raw "FileNotFoundError: landing directory
    # ... does not exist", though `engine doctor` already knows that folder.
    monkeypatch.chdir(tmp_path)
    code = main(
        ["run", "demo", "ap", "intake", "--ledger-dir", str(tmp_path / "ledger"), "--shadow"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "landing directory demo-data/inbox does not exist" in out
    assert "engine doctor demo" in out
    assert "FileNotFoundError" not in out
