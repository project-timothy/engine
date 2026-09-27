"""Independence lint: fail the build if anything under ``auditor/`` imports core.

The auditor's whole value rests on structural independence (design principle
2): a checker that shares the worker's code shares the worker's bugs, and a
shared bug passes both. Same spirit as the bleed-through lint, enforced the
same way — in CI, on every PR.

The check is AST-based, so ``import core``, ``from core.x import y``, a lazy
import inside a function, and ``importlib.import_module("core...")`` all
fail, while lookalikes (``corelib``, ``score``, relative ``.core_helpers``)
and the word "core" in strings or comments pass.

Run standalone (``python -m auditor.evals.independence_lint``) or via the
eval in ``test_independence.py``.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
# This linter names the forbidden module in its patterns, and its eval plants
# violation examples as string fixtures; both are excluded from the scan (same
# convention as the bleed-through lint exempting its own token list).
SELF_EXEMPT = {"independence_lint.py", "test_independence.py"}

_IMPORTLIB_PATTERN = re.compile(r"import_module\(\s*[\"']core(\.|[\"'])")


@dataclass(frozen=True)
class Violation:
    path: Path
    line_number: int
    detail: str


def auditor_root() -> Path:
    # auditor/evals/independence_lint.py -> parents[1] is auditor/.
    return Path(__file__).resolve().parents[1]


def _is_core(module: str | None) -> bool:
    return module is not None and (module == "core" or module.startswith("core."))


def _scan_file(path: Path) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    violations: list[Violation] = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_core(alias.name):
                    violations.append(Violation(path, node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and _is_core(node.module):
                violations.append(Violation(path, node.lineno, f"from {node.module} import ..."))
    for n, line in enumerate(source.splitlines(), 1):
        if _IMPORTLIB_PATTERN.search(line):
            violations.append(Violation(path, n, "importlib.import_module of core"))
    return violations


def scan(root: Path | None = None) -> list[Violation]:
    root = root or auditor_root()
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name in SELF_EXEMPT:
            continue
        violations.extend(_scan_file(path))
    return violations


def main() -> int:
    violations = scan()
    if not violations:
        print("independence lint: clean (nothing under auditor/ imports core)")
        return 0
    root = auditor_root()
    print(f"independence lint: {len(violations)} violation(s) under auditor/", file=sys.stderr)
    for v in violations:
        rel = v.path.relative_to(root.parent) if v.path.is_relative_to(root.parent) else v.path
        print(f"  {rel}:{v.line_number}: {v.detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
