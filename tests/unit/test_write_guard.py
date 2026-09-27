"""Unit tests for the write guard (the explicit shadow-safety mechanism)."""

from __future__ import annotations

import pytest

from core.engine.guard import ProtectedSurfaceError, WriteGuard


def test_guard_refuses_protected_root_and_children(tmp_path):
    protected = tmp_path / "production"
    protected.mkdir()
    guard = WriteGuard([protected])
    with pytest.raises(ProtectedSurfaceError):
        guard.check_write(protected)
    with pytest.raises(ProtectedSurfaceError):
        guard.check_write(protected / "ledger" / "deep" / "file.xlsx")


def test_guard_allows_unprotected_paths(tmp_path):
    guard = WriteGuard([tmp_path / "production"])
    safe = tmp_path / "engine" / "out.md"
    assert guard.check_write(safe) == safe.resolve()


def test_empty_entries_are_ignored(tmp_path):
    guard = WriteGuard(["", "  "])
    assert guard.roots == []
    assert guard.check_write(tmp_path / "x")


# ---------- allow-list carve-outs (AP cutover, 2026-07-09) -------------------


def test_allowed_carveout_permits_file_and_children_inside_protected_root(tmp_path):
    """The cutover shape: the tree stays protected; the engine's own book and
    filing dir are explicit carve-outs, so 'writable' is a decision recorded
    in config, never a hole left by unguarding the tree."""
    protected = tmp_path / "production"
    protected.mkdir()
    book = protected / "ledgers" / "engine_book.xlsx"
    filed = protected / "ledgers" / "_engine_filed"
    guard = WriteGuard([protected], allowed=[book, filed])
    assert guard.check_write(book) == book.resolve()
    assert (
        guard.check_write(filed / "01_Inbox" / "inv.pdf")
        == (filed / "01_Inbox" / "inv.pdf").resolve()
    )


def test_non_carveout_siblings_stay_refused(tmp_path):
    protected = tmp_path / "production"
    protected.mkdir()
    guard = WriteGuard([protected], allowed=[protected / "ledgers" / "_engine_filed"])
    with pytest.raises(ProtectedSurfaceError):
        guard.check_write(protected / "ledgers" / "legacy_book.xlsx")
    with pytest.raises(ProtectedSurfaceError):
        guard.check_write(protected / "reports" / "close.md")


def test_carveout_does_not_affect_is_protected(tmp_path):
    """The runner's ledger-root refusal ignores carve-outs: the engine's own
    data dir inside the production tree is a misconfiguration even when file
    writes there would be allowed."""
    protected = tmp_path / "production"
    protected.mkdir()
    carved = protected / "ledgers" / "_engine_filed"
    guard = WriteGuard([protected], allowed=[carved])
    assert guard.is_protected(carved / "sub")


def test_runner_refuses_a_ledger_root_inside_a_protected_surface(tmp_path, monkeypatch):
    """Worst misconfiguration: the engine's data dir pointed at production."""
    from core.engine.runner import run

    protected = tmp_path / "prod"
    (protected / "sub").mkdir(parents=True)
    tenants = tmp_path / "tenants" / "guarded"
    tenants.mkdir(parents=True)
    (tenants / "tenant.toml").write_text(
        f"""
[identity]
legal_name = "Guarded Inc."
slug = "guarded"

[ap]
protected_paths = ["{protected}"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(tmp_path / "tenants"))
    with pytest.raises(ProtectedSurfaceError):
        run("guarded", "demo", "ingest", ledger_dir=protected / "sub")
