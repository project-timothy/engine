"""Heartbeat lens: scheduled-run preflight refusal markers must surface.

Issue #108 (2026-08-11): scheduled runs executed from the live dev working
tree, so a merged config fix lagged a night behind (the report re-nagged
vendors the fix had already cleared) and a tree left on a feature branch
overnight would have run unreviewed code. The fix is a preflight guard in the
entry scripts; when it refuses (or degrades) a run it writes a JSON marker
under ``<store base>/preflight/``. The auditor is the delivery surface for
that refusal: a fresh marker is a CRITICAL checklist item the next time the
audit runs. Full story: docs/lessons.md, "Scheduled runs execute a deploy clone".
"""

from __future__ import annotations

import json

from auditor.lenses import heartbeat

from .fixtures import make_context, make_ledger

FRESH_TS = "2026-07-21T02:00:00Z"  # 4h before NOW — last night's run
STALE_TS = "2026-07-18T02:00:00Z"  # 76h before NOW — outside the lookback


def _ctx(tmp_path):
    # Explicit store_root mirroring production layout (<base>/<slug>) and
    # keeping the marker dir (<base>/preflight) inside this test's tmp dir —
    # make_context's default store_root lands at tmp_path.parent, which pytest
    # shares across tests.
    return make_context(tmp_path, store_root=tmp_path / ".auditor-store" / "slug")


def _marker_dir(ctx):
    return ctx.store_root.parent / "preflight"


def _write_marker(
    ctx, *, kind="refusal", job="auditor-nightly", reason="branch-not-main", ts=FRESH_TS, name=None
):
    d = _marker_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "job": job,
        "kind": kind,
        "reason": reason,
        "detail": f"synthetic {reason} marker",
        "ts": ts,
        "branch": "feature/x",
        "head": "abc123",
    }
    path = d / (name or f"{kind}-{job}-{ts.replace(':', '').replace('-', '')}.json")
    path.write_text(json.dumps(payload))
    return path


def test_no_marker_dir_is_quiet(tmp_path):
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    with ctx.ledger:
        assert heartbeat.check_preflight_refusals(ctx) == []


def test_fresh_refusal_is_critical(tmp_path):
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    _write_marker(ctx, kind="refusal", reason="branch-not-main")
    with ctx.ledger:
        findings = heartbeat.check_preflight_refusals(ctx)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "CRITICAL"
    assert f.condition == "preflight-refused"
    assert f.subject == "scheduled run auditor-nightly"
    assert "branch-not-main" in f.detail


def test_fresh_warning_is_warn(tmp_path):
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    _write_marker(ctx, kind="warning", reason="fetch-failed", job="engine-ap-daily")
    with ctx.ledger:
        findings = heartbeat.check_preflight_refusals(ctx)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "WARN"
    assert f.condition == "preflight-warning"
    assert f.subject == "scheduled run engine-ap-daily"


def test_stale_marker_outside_lookback_is_quiet(tmp_path):
    # A marker that already had its nights on the report ages out; the
    # checklist reconciliation resolves the item once it stops appearing.
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    _write_marker(ctx, ts=STALE_TS)
    with ctx.ledger:
        assert heartbeat.check_preflight_refusals(ctx) == []


def test_repeated_refusals_fold_to_one_finding_with_count(tmp_path):
    # Same job refused two nights running: one checklist item (stable
    # identity), not a pile — but the detail says how many.
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    _write_marker(ctx, ts="2026-07-21T02:00:00Z", name="refusal-a.json")
    _write_marker(ctx, ts="2026-07-20T08:00:00Z", name="refusal-b.json")
    with ctx.ledger:
        findings = heartbeat.check_preflight_refusals(ctx)
    assert len(findings) == 1
    assert "2 refusal" in findings[0].detail


def test_unreadable_marker_is_warn_not_crash(tmp_path):
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    d = _marker_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    (d / "refusal-garbage.json").write_text("{not json")
    with ctx.ledger:
        findings = heartbeat.check_preflight_refusals(ctx)
    assert len(findings) == 1
    assert findings[0].severity == "WARN"
    assert findings[0].condition == "marker-unreadable"


def test_registered_in_lens_check(tmp_path):
    # The marker check must ride the lens' main entry point, not sit orphaned.
    make_ledger(tmp_path)
    ctx = _ctx(tmp_path)
    _write_marker(ctx)
    with ctx.ledger:
        findings = heartbeat.check(ctx)
    assert any(f.condition == "preflight-refused" for f in findings)
