"""Write guard: the explicit shadow-safety mechanism.

Shadow mode is read-only toward every production surface (the tenant's
protected list). The guard makes that a checked property
rather than a convention: a job must route any file write through
``ctx.guard.check_write``, and the guard refuses paths under a protected
root. Protected roots come from tenant configuration
(``[ap].protected_paths``), so core/ never names a real path.

The runner also refuses to open a ledger whose root lies inside a protected
root, which blocks the worst misconfiguration (pointing the engine's own
data directory at a production tree) before any write happens.
"""

from __future__ import annotations

from pathlib import Path


class ProtectedSurfaceError(PermissionError):
    """A write targeted a protected production surface."""


class WriteGuard:
    def __init__(
        self,
        protected_roots: list[str | Path],
        allowed: list[str | Path] | None = None,
    ) -> None:
        """``allowed`` carves explicit write surfaces out of a protected root
        (AP cutover, 2026-07-09): the production tree stays protected wholesale
        and the engine's own delivery targets are named exceptions, so
        "writable" is a decision recorded in tenant config, never a hole left
        by unguarding the tree. Carve-outs apply to ``check_write`` only;
        ``is_protected`` ignores them, so the runner still refuses a ledger
        root anywhere inside the production tree.
        """
        self._roots = [Path(p).expanduser().resolve() for p in protected_roots if str(p).strip()]
        self._allowed = [Path(p).expanduser().resolve() for p in (allowed or []) if str(p).strip()]

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def is_protected(self, path: str | Path) -> bool:
        candidate = Path(path).expanduser().resolve()
        return any(candidate == root or root in candidate.parents for root in self._roots)

    def _is_allowed(self, candidate: Path) -> bool:
        return any(candidate == a or a in candidate.parents for a in self._allowed)

    def check_write(self, path: str | Path) -> Path:
        """Validate a write target. Returns the resolved path or raises."""
        candidate = Path(path).expanduser().resolve()
        if self.is_protected(candidate) and not self._is_allowed(candidate):
            raise ProtectedSurfaceError(
                f"refusing write to {candidate}: inside a protected production surface"
            )
        return candidate
