"""The duplicate-code ceiling: the count of copied blocks only goes down.

Pylint's duplicate-code check (pinned in the dev group) over ``core/``,
blocks of 8 lines or more. The gate fails when the count rises above
``CEILING``; when it falls, it passes and says to lower ``CEILING`` to the
new count in the same PR, so the gain is kept (2026-10-11).

The auditor is outside the scan on purpose: it imports nothing from
``core/`` (independence lint), so the helpers it needs are copied across
that boundary by design.

    uv run python -m core.evals.duplicate_code
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 16 on 2026-10-11: 13 in evals (shared fixtures not yet pulled out), 3 in code.
CEILING = 16


def command() -> list[str]:
    """The pylint invocation the gate runs, settings pinned here."""
    return [
        sys.executable,
        "-m",
        "pylint",
        "--disable=all",
        "--enable=duplicate-code",
        "--min-similarity-lines=8",
        "--score=n",
        "core",
    ]


def count(report: str) -> int:
    """How many duplicated blocks a pylint report names."""
    return sum(1 for line in report.splitlines() if ": R0801: " in line)


def verdict(found: int, ceiling: int = CEILING) -> int:
    """Exit status for ``found`` blocks against ``ceiling``, with a line saying why."""
    if found > ceiling:
        print(f"duplicate code rose to {found} blocks (ceiling {ceiling}): share it, don't copy it")
        return 1
    print(f"{found} duplicated blocks (ceiling {ceiling})")
    if found < ceiling:
        print(f"the count fell: lower CEILING to {found} in core/evals/duplicate_code.py")
    return 0


def main() -> int:
    result = subprocess.run(command(), cwd=REPO, capture_output=True, text=True, check=False)
    if result.returncode & 1 or result.returncode & 32:  # fatal or usage error
        print(result.stdout + result.stderr, end="", file=sys.stderr)
        return 1
    status = verdict(count(result.stdout))
    if status:
        print(result.stdout, end="")  # where the copies are
    return status


if __name__ == "__main__":
    sys.exit(main())
