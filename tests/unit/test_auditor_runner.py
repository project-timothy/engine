"""Audit runner: run lenses, reconcile the checklist, deliver the report.

A lens that crashes must become a CRITICAL finding, never a dead report:
silence is never the signal (docs/auditor-design.md, output contract).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from auditor.findings import Finding
from auditor.lenses import LensSpec
from auditor.runner import run_audit

NOW = datetime(2026, 7, 21, 6, 0, 0, tzinfo=UTC)


def _fixture_world(tmp_path, *, timezone="UTC"):
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True)
    (tenants / "t" / "tenant.toml").write_text(
        f'[identity]\nslug = "t"\ntimezone = "{timezone}"\n'
        f'[auditor]\nreport_dir = "{tmp_path / "reports"}"\n'
    )
    ledger = tmp_path / "ledger" / "t"
    ledger.mkdir(parents=True)
    sqlite3.connect(ledger / "ledger.sqlite3").close()
    return {
        "tenants_dir": tenants,
        "ledger_dir": tmp_path / "ledger",
        "store_dir": tmp_path / "store",
    }


def _lens(name, findings, *, external=False):
    return LensSpec(name=name, check=lambda ctx: findings, external=external)


def test_no_lenses_yields_all_clear_report_on_disk(tmp_path):
    world = _fixture_world(tmp_path)
    result = run_audit("t", lenses=[], now=NOW, **world)
    assert result.report_path is not None
    assert result.report_path.name == "audit-2026-07-21.md"
    text = result.report_path.read_text()
    assert "clear" in text.lower()
    assert result.new_count == result.open_count == result.resolved_count == 0


def test_report_date_uses_tenant_timezone(tmp_path):
    # 06:00 UTC on the 21st is 02:00 in New York on the 21st; but 03:00 UTC
    # would be the 20th evening in New York. Pin the boundary case.
    world = _fixture_world(tmp_path, timezone="America/New_York")
    early = datetime(2026, 7, 21, 3, 0, 0, tzinfo=UTC)  # 23:00 on the 20th, ET
    result = run_audit("t", lenses=[], now=early, **world)
    assert result.report_path.name == "audit-2026-07-20.md"


def test_lens_findings_flow_into_report(tmp_path):
    world = _fixture_world(tmp_path)
    f = Finding(lens="demo", subject="thing", condition="broken", severity="WARN", detail="why")
    result = run_audit("t", lenses=[_lens("demo", [f])], now=NOW, **world)
    assert result.new_count == 1
    assert "thing" in result.report_text


def test_crashing_lens_becomes_critical_finding(tmp_path):
    world = _fixture_world(tmp_path)

    def boom(ctx):
        raise RuntimeError("lens exploded")

    result = run_audit("t", lenses=[LensSpec(name="bad", check=boom)], now=NOW, **world)
    assert result.new_count == 1
    assert "CRITICAL" in result.report_text
    assert "bad" in result.report_text
    assert "lens exploded" in result.report_text


def test_local_only_skips_external_lenses(tmp_path):
    world = _fixture_world(tmp_path)
    f = Finding(lens="net", subject="s", condition="c", severity="WARN", detail="d")
    result = run_audit(
        "t", lenses=[_lens("net", [f], external=True)], now=NOW, local_only=True, **world
    )
    assert result.new_count == 0


def test_run_is_recorded_in_own_store(tmp_path):
    world = _fixture_world(tmp_path)
    run_audit("t", lenses=[], now=NOW, **world)
    db = sqlite3.connect(world["store_dir"] / "t" / "auditor.sqlite3")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM auditor_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "ok"
    assert row["report_path"].endswith("audit-2026-07-21.md")


def test_second_run_same_day_regenerates_the_same_file(tmp_path):
    world = _fixture_world(tmp_path)
    first = run_audit("t", lenses=[], now=NOW, **world)
    second = run_audit("t", lenses=[], now=NOW, **world)
    assert first.report_path == second.report_path


# ---- honesty audit 2026-09-03, findings 04-F2 and 04-F3 -----------------------


def _open_subjects(result):
    text = result.report_text
    section = text[text.index("## Open checklist") : text.index("## Resolved since")]
    return [ln for ln in section.splitlines() if ln.startswith("- [ ]")]


def test_crashed_lens_never_resolves_its_open_items(tmp_path):
    """04-F2: on a crash night the crashed lens's open items were printed as
    'resolved' (reality never re-verified them) and came back as NEW the next
    night. They must stay open, unlisted under resolved, and the report must
    say the lens did not run."""
    world = _fixture_world(tmp_path)
    f = Finding(lens="book", subject="wb-row", condition="drift", severity="CRITICAL", detail="d")
    run_audit("t", lenses=[_lens("book", [f])], now=NOW, **world)

    def boom(ctx):
        raise RuntimeError("book exploded")

    later = datetime(2026, 7, 22, 6, 0, 0, tzinfo=UTC)
    result = run_audit("t", lenses=[LensSpec(name="book", check=boom)], now=later, **world)
    assert result.resolved_count == 0
    assert "wb-row" not in result.report_text.split("## Resolved since")[1]
    assert any("wb-row" in ln for ln in _open_subjects(result))
    assert "book" in result.report_text and "not run" in result.report_text
    assert "crashed" in result.report_text


def test_local_only_run_leaves_external_lens_items_untouched(tmp_path):
    """04-F2: an interactive --local-only run against the live store must not
    resolve every open qbo/mail item and re-announce them the next night."""
    world = _fixture_world(tmp_path)
    f = Finding(lens="net", subject="bill-9", condition="vanished", severity="CRITICAL", detail="d")
    run_audit("t", lenses=[_lens("net", [f], external=True)], now=NOW, **world)
    later = datetime(2026, 7, 22, 6, 0, 0, tzinfo=UTC)
    result = run_audit(
        "t", lenses=[_lens("net", [f], external=True)], now=later, local_only=True, **world
    )
    assert result.resolved_count == 0
    assert result.open_count == 1
    assert "not run" in result.report_text and "net" in result.report_text
    assert "local-only" in result.report_text
    # the next full night: same item, silently carried forward, never "returned"
    night3 = datetime(2026, 7, 23, 6, 0, 0, tzinfo=UTC)
    full = run_audit("t", lenses=[_lens("net", [f], external=True)], now=night3, **world)
    assert full.new_count == 0
    assert full.open_count == 1


def test_failed_report_write_leaves_the_store_untouched(tmp_path, monkeypatch):
    """04-F3: the checklist was committed before the report existed, so a
    failed write consumed the NEW announcements forever. The store must
    commit only after the report is on disk; the next good night announces."""
    import pytest

    import auditor.runner as runner_mod

    world = _fixture_world(tmp_path)
    f = Finding(lens="demo", subject="thing", condition="broken", severity="CRITICAL", detail="d")

    def unwritable(*args, **kwargs):
        raise OSError("report dir unreachable")

    monkeypatch.setattr(runner_mod, "write_report", unwritable)
    with pytest.raises(OSError):
        run_audit("t", lenses=[_lens("demo", [f])], now=NOW, **world)
    monkeypatch.undo()

    db = sqlite3.connect(world["store_dir"] / "t" / "auditor.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
    assert db.execute("SELECT status FROM auditor_runs ORDER BY id DESC").fetchone()[0] == "error"

    later = datetime(2026, 7, 22, 6, 0, 0, tzinfo=UTC)
    result = run_audit("t", lenses=[_lens("demo", [f])], now=later, **world)
    assert result.new_count == 1
    assert "thing" in result.report_text.split("## Open checklist")[0]  # in NEW


def test_no_report_is_a_dry_run_that_keeps_the_announcements(tmp_path):
    """04-F3: `--no-report` reconciled and committed, then printed; the nightly
    that followed never announced those items as NEW."""
    world = _fixture_world(tmp_path)
    f = Finding(lens="demo", subject="thing", condition="broken", severity="WARN", detail="d")
    dry = run_audit("t", lenses=[_lens("demo", [f])], now=NOW, write=False, **world)
    assert dry.report_path is None
    assert dry.new_count == 1  # the preview still shows what a real run would say
    later = datetime(2026, 7, 22, 6, 0, 0, tzinfo=UTC)
    real = run_audit("t", lenses=[_lens("demo", [f])], now=later, **world)
    assert real.new_count == 1
    assert real.open_count == 1
    assert real.report_text.count("thing") >= 2  # NEW and the open checklist


def test_expired_snooze_returns_as_new_with_the_note(tmp_path):
    """Triage snooze (2026-09-04): a DEFER mutes the item until the date,
    then it returns as NEW carrying '(snooze expired)'."""
    world = _fixture_world(tmp_path)
    f = Finding(lens="demo", subject="thing", condition="broken", severity="WARN", detail="why")
    (world["tenants_dir"] / "t" / "triage.toml").write_text(
        '[findings]\nmuted = [{key = "demo/broken", snooze_until = "2026-07-22"}]\n'
    )
    muted_night = run_audit("t", lenses=[_lens("demo", [f])], now=NOW, **world)
    assert "(demo)" not in muted_night.report_text
    later = datetime(2026, 7, 22, 6, 0, 0, tzinfo=UTC)
    back = run_audit("t", lenses=[_lens("demo", [f])], now=later, **world)
    assert back.new_count == 1
    assert "thing: why (snooze expired)" in back.report_text
