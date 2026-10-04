"""Bleed-through lint: fail if anything under ``core/`` or ``auditor/`` names a tenant.

The tenant boundary (architecture 3.3, invariant 5) is what makes the engine
sellable: the same code runs a second business by changing configuration. This
lint is the automated enforcement. It walks every text file under ``core/`` —
and ``auditor/``, which is tenant-agnostic by the same rule (auditor design,
learning loop) — and fails on any case-insensitive hit against the token list
in ``bleedthrough_tokens.txt``.

Run standalone (``python -m core.evals.bleedthrough_lint``) or via the eval in
``test_bleedthrough.py``. The token file and this script are themselves skipped
during the scan: one legitimately holds the tokens, the other holds none.

The shipped token file carries shapes only (a home-directory path, a cloud
mount). A real tenant's names live in its own private repository and join the
scan through ``ENGINE_LINT_TOKENS`` (one path, or several joined by ``:``), so
the list of what must never appear is never itself published.

``--wide`` widens the scan from the code trees to everything that ships:
docs, tests, scripts, skills, the demo tenant and the top-level files. That is
the extraction gate's scan (a public export must pass it with the private
tokens loaded); CI runs it as a report until it reaches zero.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

TOKENS_FILENAME = "bleedthrough_tokens.txt"
EXTRA_TOKENS_ENV = "ENGINE_LINT_TOKENS"
# A line that must name a token to assert its absence ("no /Users/ path may
# appear") carries this marker; nothing else may.
ALLOW_MARKER = "bleedthrough: allow"
# The wide scan: every tree and top-level file a public export carries.
WIDE_DIRS = (
    "core",
    "auditor",
    "tenants/_templates",
    "tenants/demo",
    "docs",
    "tests",
    "scripts",
    "skills",
    "host",
    "evals",
)
WIDE_FILES = ("README.md", "CLAUDE.md", "Dockerfile", "compose.yaml", "pyproject.toml")
# Extensions that are text we care about; binaries and caches are skipped.
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".cfg",
    ".ini",
    ".yml",
    ".yaml",
    ".tmpl",
    ".ref",
    ".sh",
}
# Text files that ship without a telling suffix (2026-09-27).
TEXT_NAMES = {"Dockerfile", ".dockerignore", ".gitignore"}
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
# Files exempt from the scan: the token list (holds the tokens by design) and
# this linter (holds none, but excluding it removes any doubt).
SELF_EXEMPT = {TOKENS_FILENAME, "bleedthrough_lint.py"}


@dataclass(frozen=True)
class Hit:
    path: Path
    line_number: int
    token: str
    line: str


def core_root() -> Path:
    # core/evals/bleedthrough_lint.py -> parents[1] is core/.
    return Path(__file__).resolve().parents[1]


def default_roots() -> list[Path]:
    """Every tenant-agnostic tree: core/ always, auditor/ once it exists, and
    the tenant templates ``engine init`` renders (row 7.19): a generated
    tenant must carry no real business, host, or path."""
    roots = [core_root()]
    for rel in (("auditor",), ("tenants", "_templates")):
        candidate = core_root().parent.joinpath(*rel)
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def wide_roots() -> list[Path]:
    """Everything a public export ships (``--wide``)."""
    repo = core_root().parent
    roots = [repo / rel for rel in WIDE_DIRS if (repo / rel).is_dir()]
    return roots + [repo / name for name in WIDE_FILES if (repo / name).is_file()]


def _read_tokens(path: Path) -> list[str]:
    tokens: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            tokens.append(line)
    return tokens


def load_tokens(tokens_path: Path | None = None, *, extra: str | None = None) -> list[str]:
    """The shipped shapes, plus every private list ``ENGINE_LINT_TOKENS`` names.

    A named file that does not exist is an error, never a silent empty list:
    a scan that quietly ran without the private names would read as clean."""
    path = tokens_path or (Path(__file__).resolve().parent / TOKENS_FILENAME)
    tokens = _read_tokens(path)
    extra = os.environ.get(EXTRA_TOKENS_ENV, "") if extra is None else extra
    for name in filter(None, extra.split(":")):
        private = Path(name)
        if not private.is_file():
            raise FileNotFoundError(f"{EXTRA_TOKENS_ENV} names {private}, which does not exist")
        tokens.extend(t for t in _read_tokens(private) if t not in tokens)
    return tokens


def _iter_text_files(root: Path):
    if root.is_file():
        if root.name not in SELF_EXEMPT:
            yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name in SELF_EXEMPT:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def scan(
    root: Path | None = None, tokens: list[str] | None = None, *, wide: bool = False
) -> list[Hit]:
    roots = [root] if root is not None else (wide_roots() if wide else default_roots())
    tokens = tokens if tokens is not None else load_tokens()
    # Names match case-insensitively; a path shape (any token with a "/")
    # matches exactly, because "/Users/" is a macOS home and "/users/me" is an
    # API route.
    lowered = [(t, t if "/" in t else t.lower()) for t in tokens]
    hits: list[Hit] = []
    for scan_root in roots:
        for path in _iter_text_files(scan_root):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if ALLOW_MARKER in line:
                    continue
                low = line.lower()
                for original, needle in lowered:
                    if needle in (line if "/" in needle else low):
                        hits.append(
                            Hit(path=path, line_number=n, token=original, line=line.strip())
                        )
    return hits


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    wide = "--wide" in args
    tokens = load_tokens()
    hits = scan(tokens=tokens, wide=wide)
    scope = "everything a public export ships" if wide else "core/, auditor/, or the templates"
    private = "with" if os.environ.get(EXTRA_TOKENS_ENV) else "without"
    if not hits:
        print(
            f"bleed-through lint: clean (no tenant tokens under {scope}; {private} private tokens)"
        )
        return 0
    root = core_root()
    print(
        f"bleed-through lint: {len(hits)} hit(s) under {scope} ({private} private tokens)",
        file=sys.stderr,
    )
    for hit in hits:
        rel = hit.path.relative_to(root.parent)
        print(f"  {rel}:{hit.line_number}: token {hit.token!r} in: {hit.line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
