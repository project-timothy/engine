"""Accepted dependency advisories: one file, a reason and a review date each.

The CI advisory scan (pip-audit over the exported lock, security review
2026-10-03) fails on any published advisory. Some cannot be fixed the day
they land: no fixed release yet, or a fix that waits on a parent package.
Those are written down in ``.github/advisory-exceptions.toml``:

    [[exception]]
    id = "GHSA-xxxx-xxxx-xxxx"      # or a PYSEC / CVE id pip-audit reports
    package = "pyjwt"
    reason = "no fixed release; the engine never calls the vulnerable path"
    review_by = 2026-11-01

``python -m core.evals.advisory_exceptions`` prints the ``--ignore-vuln``
flags for pip-audit, one per line pair. It exits 1 when an entry is
malformed or its review date has passed: an exception is a decision with an
expiry, never a permanent mute (public issue #12).
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / ".github" / "advisory-exceptions.toml"
REQUIRED = ("id", "package", "reason", "review_by")


class ExceptionsError(ValueError):
    pass


@dataclass(frozen=True)
class AdvisoryException:
    id: str
    package: str
    reason: str
    review_by: date


def load(path: Path = DEFAULT_PATH) -> list[AdvisoryException]:
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: list[AdvisoryException] = []
    for i, raw in enumerate(data.get("exception", []), start=1):
        missing = [k for k in REQUIRED if not raw.get(k)]
        if missing:
            raise ExceptionsError(f"{path.name} entry {i}: missing {', '.join(missing)}")
        if not isinstance(raw["review_by"], date):
            raise ExceptionsError(f"{path.name} entry {i}: review_by must be a date (YYYY-MM-DD)")
        entries.append(
            AdvisoryException(
                id=str(raw["id"]),
                package=str(raw["package"]),
                reason=str(raw["reason"]),
                review_by=raw["review_by"],
            )
        )
    return entries


def ignore_args(entries: list[AdvisoryException], *, today: date) -> list[str]:
    lapsed = [e for e in entries if e.review_by < today]
    if lapsed:
        raise ExceptionsError(
            "; ".join(
                f"{e.id} ({e.package}): review date {e.review_by.isoformat()} has passed; "
                "fix it or re-decide with a new date"
                for e in lapsed
            )
        )
    args: list[str] = []
    for e in entries:
        args += ["--ignore-vuln", e.id]
    return args


def main() -> int:
    try:
        args = ignore_args(load(), today=date.today())
    except ExceptionsError as exc:
        print(f"advisory exceptions: {exc}", file=sys.stderr)
        return 1
    print(" ".join(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
