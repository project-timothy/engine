"""Learning loop v1: turn an incident into a machine-runnable eval case.

An incident (the lessons-doc entry shape: date, what happened, root cause, the
bad pattern, the fix, where it is enforced) is parsed into an ``Incident`` and
rendered into a skipped pytest stub. The convention (architecture 3.6, hard
rule 8): a new incident gets an eval before its fix merges. The richest seed
cases are hand-authored; this converter is the v1 mechanism that makes "every
incident becomes a regression test" mechanical rather than aspirational.

The rendered stub is intentionally skipped and marked ``phase2``: it names the
engine module that must exist for the assertion to run, so unskipping it is a
later phase's definition of done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER_RE = re.compile(r"^##\s+(?P<date>\d{4}-\d{2}-\d{2})\s*[—-]+\s*(?P<title>.+?)\s*$", re.M)
_FIELD_RE = re.compile(
    r"\*\*(?P<name>Pattern|Bad pattern|Fix|Enforcement|What happened|Root cause)\.\*\*"
    r"\s*(?P<body>.*?)(?=\n\*\*[A-Z]|\Z)",
    re.S,
)


@dataclass(frozen=True)
class Incident:
    date: str
    title: str
    what_happened: str = ""
    bad_pattern: str = ""
    fix: str = ""
    enforcement: str = ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "incident"


def parse_incident(markdown: str) -> Incident:
    """Parse one lessons-doc entry into an Incident.

    Tolerates the two header field styles in the corpus ("Pattern." and
    "What happened."). Raises if there is no dated ``## YYYY-MM-DD`` header.
    """
    header = _HEADER_RE.search(markdown)
    if not header:
        raise ValueError("incident text has no '## YYYY-MM-DD — title' header")

    fields: dict[str, str] = {}
    for match in _FIELD_RE.finditer(markdown):
        fields[match.group("name").lower()] = match.group("body").strip()

    return Incident(
        date=header.group("date"),
        title=header.group("title").strip(),
        what_happened=fields.get("what happened") or fields.get("pattern", ""),
        bad_pattern=fields.get("bad pattern", ""),
        fix=fields.get("fix", ""),
        enforcement=fields.get("enforcement", ""),
    )


def eval_test_name(incident: Incident) -> str:
    return f"test_{incident.date.replace('-', '_')}_{_slugify(incident.title)}"[:80]


def _comment_block(incident: Incident) -> str:
    lines = [f"# Incident {incident.date}: {incident.title}"]
    if incident.bad_pattern:
        first = incident.bad_pattern.splitlines()[0].strip()
        lines.append(f"# Bad pattern: {first}")
    if incident.fix:
        first = incident.fix.splitlines()[0].strip()
        lines.append(f"# Fix: {first}")
    return "\n".join(lines)


def render_eval_stub(incident: Incident, *, module_hint: str) -> str:
    """Render a syntactically valid, skipped Phase-2 eval stub for an incident.

    ``module_hint`` is the engine module that must exist for the assertion to
    run; it becomes the skip reason, so unskipping is a later phase's DoD.
    """
    test_name = eval_test_name(incident)
    return f'''"""Auto-generated eval stub from incident {incident.date}.

Hand-complete the fixture and assertion, then this case guards the fix.
"""

import pytest

{_comment_block(incident)}


@pytest.mark.phase2
@pytest.mark.skip(reason="Phase 2: {module_hint}")
def {test_name}():
    # TODO: build the fixture that reproduces the bad pattern, then assert the
    # bad pattern no longer occurs once {module_hint} exists.
    raise AssertionError("eval not yet implemented: {module_hint}")
'''
