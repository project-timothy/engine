"""Triage lens evals (2026-09-04): the headless triage's own SKIPPED/FAILED
markers went unread for three mornings (09-02..09-04); the report now names
them. The same lens reports dated triage.toml entries that aged out."""

from __future__ import annotations

from auditor.lenses import triage_lens

from .fixtures import make_context, make_ledger

SKIP = "# Triage 2026-07-20 — headless run SKIPPED"
FAIL = "# Triage 2026-07-20 — headless run FAILED"


def _world(tmp_path, notes: dict[str, str] | None = None, **overrides):
    make_ledger(tmp_path / "ledger")
    notes_dir = tmp_path / "reports" / "triage"
    if notes is not None:
        notes_dir.mkdir(parents=True)
        for name, body in notes.items():
            (notes_dir / name).write_text(body)
    return make_context(
        tmp_path / "ledger",
        tenants_dir=tmp_path / "tenants",
        triage_notes_dir=str(notes_dir),
        **overrides,
    )


def test_skipped_marker_on_the_newest_note_warns(tmp_path):
    ctx = _world(
        tmp_path,
        {
            "triage-2026-07-19.md": "# Triage 2026-07-19 audit\n\nreal triage\n",
            "triage-2026-07-20.md": f"{SKIP}\n\nDev tree busy (branch 'x').\n\nRun it.\n",
        },
    )
    with ctx.ledger:
        findings = triage_lens.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("triage note 2026-07-20", "triage-skipped", "WARN")
    ]
    assert "Dev tree busy (branch 'x')." in findings[0].detail


def test_failed_marker_warns(tmp_path):
    ctx = _world(tmp_path, {"triage-2026-07-20.md": f"{FAIL}\n\nrc=1\n"})
    with ctx.ledger:
        findings = triage_lens.check(ctx)
    assert [(f.subject, f.condition) for f in findings] == [
        ("triage note 2026-07-20", "triage-failed")
    ]


def test_a_real_triage_note_is_quiet_even_after_a_skip(tmp_path):
    ctx = _world(
        tmp_path,
        {
            "triage-2026-07-19.md": f"{SKIP}\n",
            "triage-2026-07-20.md": "# Triage — 2026-07-20 audit (run interactively)\n",
        },
    )
    with ctx.ledger:
        assert triage_lens.check(ctx) == []


def test_configured_notes_dir_that_is_missing_warns(tmp_path):
    ctx = _world(tmp_path, notes=None)
    with ctx.ledger:
        findings = triage_lens.check(ctx)
    assert [f.condition for f in findings] == ["tree-missing"]


def test_unconfigured_notes_dir_is_out_of_scope(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = make_context(tmp_path / "ledger", tenants_dir=tmp_path / "tenants")
    with ctx.ledger:
        assert triage_lens.check(ctx) == []
    ctx = _world(tmp_path / "b", {"triage-2026-07-20.md": f"{SKIP}\n"}, triage_enabled=False)
    with ctx.ledger:
        assert triage_lens.check(ctx) == []


def test_aged_out_entries_are_reported_once_as_info(tmp_path):
    tenants = tmp_path / "tenants" / "t"
    tenants.mkdir(parents=True)
    (tenants / "triage.toml").write_text(
        "[findings]\n"
        'muted = [{key = "filing/no-disposition", since = "2026-04-01", why = "known"}, "status"]\n'
        "[advisory.acknowledged]\n"
        '"coding-drift/Owner" = {answer = "loans", since = "2026-03-01"}\n'
        '"coding-drift/Fresh" = {answer = "recent", since = "2026-07-01"}\n'
    )
    ctx = _world(tmp_path, {"triage-2026-07-20.md": "# Triage ok\n"})
    with ctx.ledger:
        findings = triage_lens.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("acknowledgment coding-drift/Owner", "acknowledgment-aged", "INFO"),
        ("mute filing/no-disposition", "acknowledgment-aged", "INFO"),
    ]
    assert "(acknowledgment aged out; re-confirm or delete)" in findings[0].detail
    assert "2026-03-01" in findings[0].detail
    # the same key on the next night is the same fingerprint (announced once)
    with ctx.ledger:
        again = triage_lens.check(ctx)
    assert [f.fingerprint for f in again] == [f.fingerprint for f in findings]
