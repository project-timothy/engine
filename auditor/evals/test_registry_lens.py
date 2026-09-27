"""Registry-freshness lens evals (2026-09-04): the project registry TOML is
synced weekly by a job outside the engine; a stale file or a drift report
with items is worth one checklist line, read-only."""

from __future__ import annotations

import os
from datetime import datetime

from auditor.lenses import registry

from .fixtures import NOW, make_context, make_ledger

REGISTRY = '[meta]\nschema_version = "1"\n\n[[project]]\npn = "P00_0101"\n'


def _world(
    tmp_path, *, age_hours=1.0, drift: dict[str, str] | None = None, path_exists=True, **overrides
):
    make_ledger(tmp_path / "ledger")
    ctx_dir = tmp_path / "context"
    ctx_dir.mkdir()
    path = ctx_dir / "project-registry.toml"
    if path_exists:
        path.write_text(REGISTRY)
        stamp = datetime.fromisoformat(NOW).timestamp() - age_hours * 3600
        os.utime(path, (stamp, stamp))
    for name, body in (drift or {}).items():
        (ctx_dir / name).write_text(body)
    return make_context(tmp_path / "ledger", registry_path=str(path), **overrides)


def test_fresh_registry_without_drift_is_quiet(tmp_path):
    ctx = _world(tmp_path)
    with ctx.ledger:
        assert registry.check(ctx) == []


def test_registry_older_than_the_window_warns(tmp_path):
    ctx = _world(tmp_path, age_hours=200)
    with ctx.ledger:
        findings = registry.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("project registry", "registry-stale", "WARN")
    ]
    assert "200h" in findings[0].detail


def test_newest_drift_report_with_items_is_info(tmp_path):
    older = "# Drift 2026-07-05\n\n**Drift items:** 9\n\n- nine things\n"
    newest = (
        "# Project Registry Drift 2026-07-19\n\n**Sources scanned:** a, b\n"
        "**Total projects in registry:** 36\n**Drift items:** 2\n\n"
        "## Missing folders\n\n- P26_2031 Variant: tracked but no folder.\n"
        "- P26_2034 Comparison: tracked but no folder.\n\n## Stale\n\n(none)\n"
    )
    ctx = _world(
        tmp_path,
        drift={
            "project-registry-drift-2026-07-05.md": older,
            "project-registry-drift-2026-07-19.md": newest,
        },
    )
    with ctx.ledger:
        findings = registry.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("project registry drift", "registry-drift", "INFO")
    ]
    assert "2 item(s)" in findings[0].detail
    assert "project-registry-drift-2026-07-19.md" in findings[0].detail
    assert "P26_2031 Variant" in findings[0].detail
    assert "nine things" not in findings[0].detail


def test_drift_report_with_zero_items_is_quiet(tmp_path):
    ctx = _world(
        tmp_path,
        drift={"project-registry-drift-2026-07-19.md": "# Drift\n\n**Drift items:** 0\n\n(none)\n"},
    )
    with ctx.ledger:
        assert registry.check(ctx) == []


def test_missing_registry_file_warns(tmp_path):
    ctx = _world(tmp_path, path_exists=False)
    with ctx.ledger:
        findings = registry.check(ctx)
    assert [f.condition for f in findings] == ["registry-missing"]


def test_unconfigured_or_disabled_is_out_of_scope(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = make_context(tmp_path / "ledger")
    with ctx.ledger:
        assert registry.check(ctx) == []
    ctx = _world(tmp_path / "b", age_hours=500, registry_enabled=False)
    with ctx.ledger:
        assert registry.check(ctx) == []
