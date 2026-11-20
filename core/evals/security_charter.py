"""Security charter gate: fail the build when a rule has no test (chunk 4).

``docs/security/charter.md`` names the rules that must never break, one
``### SEC-NN: ...`` heading each, with a ``Proven by:`` line naming the test
files. A test proves a rule by carrying its marker,
``@pytest.mark.security("SEC-NN")``, so the tie lives in the test and moves
with it. The gate fails:

- a rule no test carries;
- a marker that names no rule, or a rule the charter does not have;
- a ``Proven by:`` line that disagrees with where the markers really are.

The scan is AST-based over the suite's test paths, so a rule id in a string
or a comment is no marker. Run standalone
(``python -m core.evals.security_charter``) or via ``test_security_charter.py``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

CHARTER = Path("docs") / "security" / "charter.md"
TEST_PATHS = ("tests", "core", "auditor")
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}

_RULE = re.compile(r"^### (SEC-\d{2}):", re.M)
_PROVEN_BY = re.compile(r"^Proven by: (.+)$", re.M)


def repo_root() -> Path:
    # core/evals/security_charter.py -> parents[2] is the repository.
    return Path(__file__).resolve().parents[2]


def rules(root: Path) -> dict[str, set[str] | None]:
    """Each rule id, with the files its ``Proven by:`` line names (None when
    the rule has no such line)."""
    text = (root / CHARTER).read_text(encoding="utf-8")
    heads = list(_RULE.finditer(text))
    found: dict[str, set[str] | None] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        line = _PROVEN_BY.search(text, head.end(), end)
        found[head.group(1)] = (
            {p.strip() for p in line.group(1).split(",") if p.strip()} if line else None
        )
    return found


def _is_security_marker(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "security"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "mark"
    )


def markers(root: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Which files carry each rule's marker, and the markers that name no rule."""
    where: dict[str, set[str]] = defaultdict(set)
    bad: list[str] = []
    for top in TEST_PATHS:
        if not (root / top).is_dir():
            continue
        for path in sorted((root / top).rglob("test_*.py")):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for deco in node.decorator_list:
                    if not _is_security_marker(deco):
                        continue
                    assert isinstance(deco, ast.Call)
                    ids = [a.value for a in deco.args if isinstance(a, ast.Constant)]
                    if not ids or len(ids) != len(deco.args):
                        bad.append(f"{rel}:{deco.lineno}: the security marker names no rule")
                    for rule in ids:
                        where[str(rule)].add(rel)
    return where, bad


def check(root: Path | None = None) -> list[str]:
    """Every way the charter and the tests disagree; empty when they hold."""
    root = root or repo_root()
    charter = rules(root)
    where, problems = markers(root)
    for rule, listed in charter.items():
        actual = where.get(rule, set())
        if not actual:
            problems.append(f"{rule}: no test carries @pytest.mark.security({rule!r})")
        elif listed is None:
            problems.append(
                f"{rule}: no 'Proven by:' line (the tests are {', '.join(sorted(actual))})"
            )
        elif listed != actual:
            problems.append(
                f"{rule}: 'Proven by:' says {', '.join(sorted(listed))};"
                f" the markers are in {', '.join(sorted(actual))}"
            )
    for rule in sorted(set(where) - set(charter)):
        problems.append(f"{rule}: a test carries this marker, and the charter has no such rule")
    return problems


def main() -> int:
    problems = check()
    if not problems:
        print(f"security charter: clean ({len(rules(repo_root()))} rules, each proven by a test)")
        return 0
    print(f"security charter: {len(problems)} problem(s)", file=sys.stderr)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
