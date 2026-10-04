"""The delivered workbook renders when the ledger changes, not once a morning (#326).

The proposal (docs/proposals/2026-09-20-the-workbook-renders-when-the-ledger-
changes.md) moves one trigger. The AP store marks the view stale after every
committed write, including a hand backfill outside any job run, which is how
two of the six CRITICALs of 2026-09-18 were made and which no run-level hook
can see. The end of any engine invocation renders it once, and so does the
15-minute retries job, so a backfill is caught within a quarter hour.

The mark is a file in the ledger's own ``.git/`` directory: git never tracks
it, so it is never committed and needs no schema change. The render is the
existing ``ap workbook`` job, idempotent on its key, so a write that moves
nothing the view shows renders nothing. The rules from the proposal:

- never render before the commit that made the view stale (the drain runs at
  the end of the invocation);
- never fail or roll back an AP write: a render that raises is an anomaly and
  leaves the mark for the next invocation;
- never render in shadow;
- at most once per invocation, however many rows moved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MARK = "engine-view-stale"


@dataclass
class DrainResult:
    rendered: int = 0
    anomaly: str = ""


class ViewRefresh:
    """The per-invocation contract: many marks, one render, never a raise."""

    def __init__(self, tenant: str, *, render: Callable[[str], None], rendered_key: str | None):
        self.tenant = tenant
        self.render = render
        self.rendered_key = rendered_key
        self._latest: str | None = None

    def mark_dirty(self, key: str) -> None:
        self._latest = key

    def drain(self) -> DrainResult:
        if self._latest is None or self._latest == self.rendered_key:
            self._latest = None
            return DrainResult()
        try:
            self.render(self.tenant)
        except Exception as exc:  # the photograph never eats the book
            return DrainResult(anomaly=f"view refresh failed: {type(exc).__name__}: {exc}")
        self.rendered_key, self._latest = self._latest, None
        return DrainResult(rendered=1)


# ---- the stale mark, across invocations ---------------------------------------------


def _mark_path(root: Path) -> Path | None:
    git = Path(root) / ".git"
    return git / MARK if git.is_dir() else None


def mark_stale(root: Path) -> None:
    """Called by the AP store after a committed write."""
    path = _mark_path(root)
    if path is not None:
        path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")


def is_stale(root: Path) -> bool:
    path = _mark_path(root)
    return path is not None and path.is_file()


def clear(root: Path) -> None:
    path = _mark_path(root)
    if path is not None and path.is_file():
        path.unlink()


def _render(tenant: str, *, ledger_dir: str | Path | None = None) -> None:
    """The existing workbook job (idempotent on its key). An error result
    raises, so the drain reports it and keeps the mark."""
    from ...engine.runner import run

    result = run(tenant, "ap", "workbook", ledger_dir=ledger_dir)
    if result.status == "error":
        raise RuntimeError(result.summary or "the workbook job ended in error")


def drain(tenant: str, *, ledger_dir: str | Path | None = None) -> DrainResult:
    """End of an invocation: render the view once if an AP write left it stale."""
    from ...engine.config import load_tenant
    from ...engine.runner import resolve_ledger_root

    root = resolve_ledger_root(tenant, ledger_dir)
    if not is_stale(root):
        return DrainResult()
    cfg = load_tenant(tenant)
    if not cfg.ap.workbook_path or not cfg.ap.workbook_columns:
        clear(root)  # no view to keep current
        return DrainResult()
    stamp = _mark_path(root).read_text(encoding="utf-8")
    refresh = ViewRefresh(
        tenant, render=lambda t: _render(t, ledger_dir=ledger_dir), rendered_key=None
    )
    refresh.mark_dirty(stamp)
    result = refresh.drain()
    if result.rendered:
        clear(root)
    return result
