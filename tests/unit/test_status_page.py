"""The read-only status page (phase 7 row 7.24, issue #233).

One self-contained HTML file rendered from what the ledger already holds:
the last run of every job, the approval cards still pending, and the NEW
section of the newest audit report. The page is a READER. It opens the
ledger through a ``mode=ro`` URI connection, so the whole module could not
write if it tried: no run row, no event, no record, no git commit, and no
migration on a ledger whose schema is behind the code.

Every fixture here is built by running the demo agent for real, so the rows
the page renders are rows the engine wrote.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.engine.cli import main
from core.engine.config import load_tenant
from core.engine.runner import resolve_ledger_root
from core.engine.status import (
    STATUS_DIRNAME,
    STATUS_FILENAME,
    StatusPageError,
    default_out_path,
    render_status,
)
from core.ledger import Ledger

LANDING = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"
AT = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)

REPORT_WITH_NEW = """# Auditor report — Demo Tenant Inc. — 2026-09-16

## New since the last report

- **WARN** (materiality) Vendor A invoice 1001: 4.1x the vendor median
- **INFO** (check-gaps) checks 1xxx: gap at 1049

## Open checklist

- [ ] WARN since 2026-09-01 (filing) invoice 900: no filed copy

## Resolved since the last report

- [x] (reconcile) invoice 880: cleared
"""

REPORT_ALL_CLEAR = """# Auditor report — Demo Tenant Inc. — 2026-09-15

All clear: nothing new, the checklist is empty, nothing changed overnight.
"""


def _ledger_root(tmp_path):
    return resolve_ledger_root("demo", tmp_path)


def _seed_cards(tmp_path):
    """A shadow AP intake parks pending cards of two types."""
    assert (
        main(
            [
                "run",
                "demo",
                "ap",
                "intake",
                "--shadow",
                "--ledger-dir",
                str(tmp_path),
                "--param",
                f"landing_dir={LANDING}",
                "--param",
                "extractor=fixture",
            ]
        )
        == 0
    )


def _seed_ok_run(tmp_path):
    assert main(["run", "demo", "demo", "ingest", "--ledger-dir", str(tmp_path)]) == 0


def _seed_failed_run_with_retries(tmp_path):
    """demo/flaky past its budget: three error run rows, the last retry
    record exhausted."""
    assert (
        main(
            [
                "run",
                "demo",
                "demo",
                "flaky",
                "--ledger-dir",
                str(tmp_path),
                "--param",
                "fail_times=5",
            ]
        )
        == 1
    )
    for _ in range(2):
        main(["jobs", "resume", "demo", "--now", "--ledger-dir", str(tmp_path)])


def _seed(tmp_path):
    _seed_ok_run(tmp_path)
    _seed_cards(tmp_path)
    _seed_failed_run_with_retries(tmp_path)


def _reports_dir(tmp_path, *, both: bool = True) -> Path:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if both:
        (reports / "audit-2026-09-15.md").write_text(REPORT_ALL_CLEAR, encoding="utf-8")
    (reports / "audit-2026-09-16.md").write_text(REPORT_WITH_NEW, encoding="utf-8")
    return reports


def _render(tmp_path, *, report_dir=None, at=AT) -> str:
    return render_status(
        load_tenant("demo"),
        ledger_root=_ledger_root(tmp_path),
        report_dir=report_dir,
        at=at,
        engine_commit="abc1234",
    )


# ---- the three sections --------------------------------------------------


def test_the_page_is_one_self_contained_html_file(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert html.startswith("<!doctype html>")
    assert "<style>" in html  # inline CSS
    assert "<script" not in html  # no JS
    assert "http://" not in html and "https://" not in html  # no external asset


def test_the_last_run_of_every_job_appears_once_with_the_failed_one_first(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))

    assert 'data-section="runs"' in html
    assert html.count('data-job="demo/flaky"') == 1
    assert html.count('data-job="demo/ingest"') == 1
    assert html.count('data-job="ap/intake"') == 1
    # the failed job sorts above the ones that landed ok
    assert html.index('data-job="demo/flaky"') < html.index('data-job="demo/ingest"')
    assert 'data-status="error"' in html


def test_a_run_row_carries_its_status_run_key_and_landed_time(tmp_path):
    _seed(tmp_path)
    with Ledger.open(_ledger_root(tmp_path)) as ledger:
        row = ledger.conn.execute(
            "SELECT idempotency_key, created_at FROM runs WHERE job = 'ingest' ORDER BY id DESC"
        ).fetchone()
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert row["idempotency_key"] in html
    # the tenant's wall clock, not the raw UTC stamp
    assert row["created_at"] not in html
    assert "2026-09-16" in html


def test_the_retry_state_rides_the_row_when_the_record_carries_one(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert "attempt 3 of 3" in html
    assert "exhausted" in html


def test_a_job_with_no_retry_record_says_nothing_about_retries(tmp_path):
    _seed_ok_run(tmp_path)
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    start = html.index('data-job="demo/ingest"')
    row = html[start : html.index("</tr>", start)]
    assert "attempt" not in row
    assert "exhausted" not in row


def test_pending_cards_are_grouped_by_type_with_a_count_and_the_oldest_age(tmp_path):
    _seed(tmp_path)
    with Ledger.open(_ledger_root(tmp_path)) as ledger:
        pending = ledger.list_approvals("demo", status="pending")
    types = {row["action_type"] for row in pending}
    assert len(types) >= 2, "the fixture must park cards of at least two types"

    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert 'data-section="cards"' in html
    for action_type in types:
        assert f'data-card-type="{action_type}"' in html
    counted = sum(1 for row in pending if row["action_type"] == sorted(types)[0])
    assert f'data-count="{counted}"' in html
    assert "oldest" in html


def test_each_pending_card_carries_one_summary_line(tmp_path):
    _seed(tmp_path)
    with Ledger.open(_ledger_root(tmp_path)) as ledger:
        pending = ledger.list_approvals("demo", status="pending")
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    for row in pending:
        assert f'data-card-id="{row["id"]}"' in html


def test_a_tin_shaped_card_value_never_reaches_the_page(tmp_path):
    _seed(tmp_path)
    with Ledger.open(_ledger_root(tmp_path)) as ledger:
        run_id = ledger.conn.execute("SELECT id FROM runs ORDER BY id LIMIT 1").fetchone()["id"]
        ledger.enqueue_approval(
            idempotency_key="w9-planted",
            run_id=run_id,
            tenant="demo",
            agent="ap",
            action_type="ap.w9_file_and_flip",
            params={"vendor": "Acme LLC", "note": "TIN 12-3456789 read off the form"},
        )
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert "12-3456789" not in html
    assert "redacted:tin" in html


def test_the_new_section_of_the_newest_report_is_on_the_page(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert 'data-section="audit"' in html
    assert "4.1x the vendor median" in html
    assert "audit-2026-09-16.md" in html
    # the newest report only, and only its NEW section
    assert "gap at 1049" in html
    assert "no filed copy" not in html  # the open checklist is not this page's job
    assert "nothing changed overnight" not in html  # yesterday's report


def test_a_report_with_no_new_section_says_so_rather_than_failing(tmp_path):
    _seed(tmp_path)
    reports = tmp_path / "quiet"
    reports.mkdir()
    (reports / "audit-2026-09-16.md").write_text(REPORT_ALL_CLEAR, encoding="utf-8")
    html = _render(tmp_path, report_dir=reports)
    assert "no NEW section" in html


def test_a_missing_report_directory_renders_a_notice(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=tmp_path / "nowhere")
    assert 'data-section="audit"' in html
    assert "no audit report" in html.lower()
    assert "nowhere" in html


def test_no_report_directory_configured_renders_a_notice(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=None)
    assert "no audit report" in html.lower()


def test_an_empty_report_directory_renders_a_notice(tmp_path):
    _seed(tmp_path)
    empty = tmp_path / "empty-reports"
    empty.mkdir()
    html = _render(tmp_path, report_dir=empty)
    assert "no audit report" in html.lower()


def test_the_footer_names_the_tenant_the_commit_the_time_and_read_only(tmp_path):
    _seed(tmp_path)
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    footer = html[html.index("<footer") :]
    assert "demo" in footer
    assert "abc1234" in footer
    assert "2026-09-16" in footer
    assert "read-only" in footer


def test_an_empty_ledger_renders_every_section_as_empty(tmp_path):
    with Ledger.open(_ledger_root(tmp_path)):
        pass
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert "no runs" in html.lower()
    assert "no cards" in html.lower()


def test_html_in_a_value_is_escaped_not_rendered(tmp_path):
    _seed_ok_run(tmp_path)
    with Ledger.open(_ledger_root(tmp_path)) as ledger:
        run_id = ledger.conn.execute("SELECT id FROM runs ORDER BY id LIMIT 1").fetchone()["id"]
        ledger.enqueue_approval(
            idempotency_key="xss",
            run_id=run_id,
            tenant="demo",
            agent="ap",
            action_type="ap.file_invoice",
            params={"vendor": "<script>alert(1)</script>"},
        )
    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---- no write path -------------------------------------------------------


def _fingerprint(root: Path) -> dict:
    db = (root / "ledger.sqlite3").read_bytes()
    log = (root / "event_log.jsonl").read_bytes()
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        "db": hashlib.sha256(db).hexdigest(),
        "log": hashlib.sha256(log).hexdigest(),
        "head": head,
        "status": status,
        "files": sorted(p.name for p in root.iterdir()),
    }


def test_rendering_writes_nothing_to_the_ledger(tmp_path):
    _seed(tmp_path)
    root = _ledger_root(tmp_path)
    before = _fingerprint(root)
    _render(tmp_path, report_dir=_reports_dir(tmp_path))
    _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert _fingerprint(root) == before


def test_the_connection_itself_refuses_a_write(tmp_path):
    from core.engine.status import read_only_connection

    _seed_ok_run(tmp_path)
    conn = read_only_connection(_ledger_root(tmp_path))
    try:
        with pytest.raises(Exception) as exc:
            conn.execute("DELETE FROM runs")
        assert "readonly" in str(exc.value).lower()
    finally:
        conn.close()


def test_a_ledger_behind_the_current_schema_renders_and_is_not_migrated(tmp_path):
    """A v5 ledger has no job_records at all. The page renders its runs and
    says nothing about retries; nothing migrates it on the way past."""
    import sqlite3

    from core.ledger.schema import MIGRATIONS

    root = _ledger_root(tmp_path)
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    conn = sqlite3.connect(str(root / "ledger.sqlite3"))
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, statements in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version > 5:
            break
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-01-01')",
            (version,),
        )
    conn.execute(
        "INSERT INTO runs (idempotency_key, tenant, agent, job, status, shadow, "
        "result_json, summary, created_at) VALUES "
        "('k1', 'demo', 'demo', 'ingest', 'ok', 0, '{}', 'ingested 2 items', "
        "'2026-09-16T12:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    (root / "event_log.jsonl").write_text("", encoding="utf-8")

    html = _render(tmp_path, report_dir=_reports_dir(tmp_path))
    assert 'data-job="demo/ingest"' in html

    check = sqlite3.connect(str(root / "ledger.sqlite3"))
    versions = {r[0] for r in check.execute("SELECT version FROM schema_migrations")}
    tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert versions == {1, 2, 3, 4, 5}
    assert "job_records" not in tables


def test_a_ledger_that_does_not_exist_is_a_clean_refusal(tmp_path):
    with pytest.raises(StatusPageError) as exc:
        render_status(load_tenant("demo"), ledger_root=tmp_path / "no-such-ledger")
    assert "no ledger" in str(exc.value)


# ---- the command ---------------------------------------------------------


def test_status_page_stdout_prints_the_html(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    code = main(
        [
            "status-page",
            "demo",
            "--stdout",
            "--ledger-dir",
            str(tmp_path),
            "--report-dir",
            str(_reports_dir(tmp_path)),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("<!doctype html>")


def test_status_page_out_writes_the_file_and_prints_its_path(tmp_path, capsys):
    _seed(tmp_path)
    target = tmp_path / "out" / "status.html"
    capsys.readouterr()
    code = main(
        [
            "status-page",
            "demo",
            "--out",
            str(target),
            "--ledger-dir",
            str(tmp_path),
            "--report-dir",
            str(_reports_dir(tmp_path)),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert str(target) in out


def test_the_owner_write_back_status_command_still_exists(tmp_path, capsys):
    """`engine status <tenant> <ref> --scheduled` is the owner's write-back
    and keeps its name; the page is `status-page`."""
    code = main(["status", "demo", "NO-SUCH-INVOICE", "--paid", "--ledger-dir", str(tmp_path)])
    assert code == 2
    assert "status-page" not in capsys.readouterr().err


def test_the_report_directory_comes_from_the_tenants_own_auditor_table(tmp_path):
    """core/ imports nothing from auditor/, so the page reads the one key it
    needs out of tenant.toml with its own parser (added with the green
    commit: the helper the CLI calls to resolve --report-dir's default)."""
    from core.engine.status import auditor_report_dir

    root = tmp_path / "tenants"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "tenant.toml").write_text(
        '[auditor]\nreport_dir = "/tmp/acme-reports"\n', encoding="utf-8"
    )
    (root / "bare").mkdir(parents=True)
    (root / "bare" / "tenant.toml").write_text("[identity]\nslug = 'bare'\n", encoding="utf-8")

    assert auditor_report_dir("acme", tenants_root=root) == "/tmp/acme-reports"
    assert auditor_report_dir("bare", tenants_root=root) == ""
    assert auditor_report_dir("missing", tenants_root=root) == ""


def test_the_default_out_path_sits_under_the_tenants_report_tree():
    tenant = load_tenant("demo")
    path = default_out_path(tenant)
    assert path is not None
    assert path.parent.name == STATUS_DIRNAME
    assert path.name == STATUS_FILENAME
    assert str(path).startswith(tenant.close.report_dir)
