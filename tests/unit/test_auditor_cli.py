"""CLI contract: ``auditor run <tenant>`` and the checklist view.

Exit codes mirror the engine CLI: 0 for a completed audit (findings are the
owner's checklist, not a process failure), 1 for an audit error, 2 for usage
or configuration errors.
"""

from __future__ import annotations

import sqlite3

from auditor.cli import main
from auditor.evals.fixtures import init_ledger_repo, make_ledger


def _fixture_world(tmp_path):
    """A CLEAN world: real ledger DDL and a pushed git repo, so the full
    default lens registry runs and finds nothing."""
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True)
    (tenants / "t" / "tenant.toml").write_text(
        '[identity]\nslug = "t"\ntimezone = "UTC"\n'
        f'[auditor]\nreport_dir = "{tmp_path / "reports"}"\n'
    )
    ledger = tmp_path / "ledger" / "t"
    make_ledger(ledger).close()
    init_ledger_repo(ledger)
    return [
        "--tenants-dir",
        str(tenants),
        "--ledger-dir",
        str(tmp_path / "ledger"),
        "--store-dir",
        str(tmp_path / "store"),
    ]


def test_unknown_tenant_exits_two(tmp_path, capsys):
    code = main(["run", "ghost", "--tenants-dir", str(tmp_path)])
    assert code == 2
    assert "ghost" in capsys.readouterr().err


def test_missing_ledger_exits_two(tmp_path, capsys):
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True)
    (tenants / "t" / "tenant.toml").write_text('[identity]\nslug = "t"\n')
    code = main(["run", "t", "--tenants-dir", str(tenants), "--ledger-dir", str(tmp_path / "no")])
    assert code == 2


def test_run_writes_report_and_exits_zero(tmp_path, capsys):
    code = main(["run", "t", *_fixture_world(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "audit-" in out  # tells the owner where the report landed
    reports = list((tmp_path / "reports").glob("audit-*.md"))
    assert len(reports) == 1


def test_run_without_report_dir_exits_two(tmp_path, capsys):
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True)
    (tenants / "t" / "tenant.toml").write_text('[identity]\nslug = "t"\n')
    ledger = tmp_path / "ledger" / "t"
    ledger.mkdir(parents=True)
    sqlite3.connect(ledger / "ledger.sqlite3").close()
    code = main(
        [
            "run",
            "t",
            "--tenants-dir",
            str(tenants),
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--store-dir",
            str(tmp_path / "store"),
        ]
    )
    assert code == 2
    assert "report_dir" in capsys.readouterr().err


def test_checklist_shows_open_items(tmp_path, capsys):
    args = _fixture_world(tmp_path)
    main(["run", "t", *args])
    code = main(["checklist", "t", *args])
    out = capsys.readouterr().out
    assert code == 0
    assert "empty" in out.lower()  # clean fixture world: nothing open


def test_lenses_lists_the_registry(capsys):
    code = main(["lenses"])
    assert code == 0


def test_no_report_help_says_it_is_a_dry_run(capsys):
    """04-F3: --no-report no longer consumes NEW announcements; the help says so."""
    from auditor.cli import build_parser

    help_text = build_parser()._subparsers._group_actions[0].choices["run"].format_help()
    assert "dry run" in help_text
