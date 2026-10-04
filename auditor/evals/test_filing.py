"""Filing-coverage lens evals: every arrival has a story, or the owner hears."""

from __future__ import annotations

import os
from datetime import datetime

from auditor.lenses import filing

from .fixtures import NOW, add_approval, add_event, make_context, make_ledger


def _landing_file(landing, name, *, age_days: float, subdir=""):
    target_dir = landing / subdir if subdir else landing
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    path.write_bytes(b"pdf")
    stamp = datetime.fromisoformat(NOW).timestamp() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _ctx(tmp_path, landing, **overrides):
    return make_context(tmp_path / "ledger", landing_dir=str(landing), **overrides)


def _world(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    landing = tmp_path / "landing"
    landing.mkdir()
    return conn, landing


def test_file_with_an_event_mention_is_covered(tmp_path):
    conn, landing = _world(tmp_path)
    _landing_file(landing, "inv.pdf", age_days=5)
    add_event(conn, event_type="ap.invoice.recorded", payload={"file": "inv.pdf"})
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_mention_via_full_path_in_a_card_counts(tmp_path):
    conn, landing = _world(tmp_path)
    _landing_file(landing, "inv.pdf", age_days=5)
    add_approval(conn, params={"source": f"{landing}/inv.pdf"})
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_unmentioned_file_is_a_finding(tmp_path):
    _, landing = _world(tmp_path)
    _landing_file(landing, "mystery.pdf", age_days=5)
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        findings = filing.check(ctx)
    assert [f.condition for f in findings] == ["no-disposition"]
    assert findings[0].subject == "mystery.pdf"


def test_fresh_arrival_gets_the_grace_window(tmp_path):
    _, landing = _world(tmp_path)
    _landing_file(landing, "overnight.pdf", age_days=0.5)
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_legacy_files_predating_the_engine_are_out_of_scope(tmp_path):
    _, landing = _world(tmp_path)
    _landing_file(landing, "ancient.pdf", age_days=400)
    ctx = _ctx(tmp_path, landing, coverage_since="2026-07-09")
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_archived_files_need_a_story_too(tmp_path):
    _, landing = _world(tmp_path)
    _landing_file(landing, "buried.pdf", age_days=6, subdir="_archive/2026-07")
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        findings = filing.check(ctx)
    assert [f.condition for f in findings] == ["no-disposition"]
    assert "archive" in findings[0].detail


def test_covered_but_stalled_at_top_level_is_a_finding(tmp_path):
    conn, landing = _world(tmp_path)
    _landing_file(landing, "waiting.pdf", age_days=20)
    add_event(conn, event_type="mail.attachment_saved", payload={"file": "waiting.pdf"})
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        findings = filing.check(ctx)
    assert [f.condition for f in findings] == ["stalled-at-top-level"]


def test_archived_old_files_are_not_stalled(tmp_path):
    conn, landing = _world(tmp_path)
    _landing_file(landing, "done.pdf", age_days=40, subdir="_archive/2026-06")
    add_event(conn, event_type="ap.landing.archived", payload={"file": "done.pdf"})
    ctx = _ctx(tmp_path, landing, coverage_since="")
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_os_droppings_are_ignored(tmp_path):
    _, landing = _world(tmp_path)
    _landing_file(landing, ".DS_Store", age_days=30)
    _landing_file(landing, ".hidden", age_days=30)
    ctx = _ctx(tmp_path, landing)
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_no_landing_dir_means_nothing_to_audit(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = make_context(tmp_path / "ledger", landing_dir="")
    with ctx.ledger:
        assert filing.check(ctx) == []


def test_configured_landing_dir_that_is_missing_is_a_warn_not_silence(tmp_path):
    """Honesty audit 2026-09-03 (04-F4): 'not configured' and 'configured but
    gone' collapsed into the same silent branch, so a renamed or unmounted
    landing folder read as a clean night and resolved the open filing items."""
    make_ledger(tmp_path / "ledger")
    ctx = make_context(tmp_path / "ledger", landing_dir=str(tmp_path / "gone"))
    with ctx.ledger:
        findings = filing.check(ctx)
    assert [f.condition for f in findings] == ["tree-missing"]
    assert findings[0].severity == "WARN"
    assert str(tmp_path / "gone") in findings[0].detail
