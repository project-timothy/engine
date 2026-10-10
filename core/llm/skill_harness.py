"""Skill regression harness: the contract a prose skill's note must keep.

A prose skill (a ``SKILL.md`` a coding-agent session executes) cannot be
run in CI: it needs a model, a key, and a live world. What CAN run in CI is
the contract around it, which the headless wrappers and the morning brief
already depend on:

- the note the skill writes carries these section headings, in this order;
- the note never contains these strings (things the skill is forbidden to
  do, so their appearance in a note is evidence it did them);
- the note's first line never carries a word that marks it unfinished (the
  wrappers treat such a note as a failed run);
- the ``SKILL.md`` itself still names every required section and every
  guardrail marker, so an edit that renames a section fails statically.

The contract is a plain TOML file named ``contract.toml`` beside each
``SKILL.md``::

    name = "example-skill"
    required_sections = ["Findings", "Actions", "Watch list"]
    banned_strings = ["gh pr merge"]
    first_line_never = ["preliminary"]
    skill_must_mention = ["never merge", "Watch list"]

Section matching is deliberately lenient about markdown shape: a heading is
any ``#`` line or a line that is only bold text (optionally numbered), and a
required section matches a heading whose text starts with it, case- and
whitespace-insensitive. So ``## Matched (unattended)`` satisfies ``Matched``
and ``**Tie-out**`` satisfies ``Tie-out``. Banned-string checks are plain
case-insensitive substring tests over the whole note.

No tenant, person, path, or business appears here (invariant 5); everything
tenant-shaped lives in the contract files under ``tenants/``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*(?:\d+[.)]\s+)?\*\*(.+?)\*\*:?\s*$")


@dataclass(frozen=True)
class Contract:
    """What a skill promises about its note and about its own text."""

    name: str
    required_sections: list[str] = field(default_factory=list)
    banned_strings: list[str] = field(default_factory=list)
    first_line_never: list[str] = field(default_factory=list)
    skill_must_mention: list[str] = field(default_factory=list)


def load_contract(path: Path) -> Contract:
    """Parse ``contract.toml``. Unknown keys are an error: a typo in a field
    name must not silently switch a check off."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(Contract)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{path}: unknown contract field(s): {', '.join(unknown)}")
    if "name" not in data:
        raise ValueError(f"{path}: contract needs a name")
    for key in known - {"name"}:
        values = data.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{path}: {key} must be a list of strings")
    return Contract(**data)


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def headings(note_text: str) -> list[str]:
    """Every heading in the note, in document order, as written."""
    found: list[str] = []
    for line in note_text.splitlines():
        m = _MD_HEADING.match(line) or _BOLD_HEADING.match(line)
        if m:
            found.append(m.group(1).strip())
    return found


def first_line(note_text: str) -> str:
    for line in note_text.splitlines():
        if line.strip():
            return line
    return ""


def check_note(note_text: str, contract: Contract) -> list[str]:
    """Violations of the note contract; an empty list means the note passes.

    One violation per missing or out-of-order section, one per banned string
    found, one per unfinished-marker word in the first line.
    """
    violations: list[str] = []

    found = [_normalize(h) for h in headings(note_text)]
    cursor = 0
    for section in contract.required_sections:
        want = _normalize(section)
        at = next((i for i in range(cursor, len(found)) if found[i].startswith(want)), None)
        if at is not None:
            cursor = at + 1
            continue
        anywhere = any(h.startswith(want) for h in found)
        if anywhere:
            violations.append(f"section {section!r} is out of order")
        else:
            violations.append(f"section {section!r} is missing")

    low = note_text.lower()
    for banned in contract.banned_strings:
        if banned.lower() in low:
            violations.append(f"banned string {banned!r} appears in the note")

    head = first_line(note_text).lower()
    for marker in contract.first_line_never:
        if marker.lower() in head:
            violations.append(f"first line still carries {marker!r} (unfinished note)")

    return violations


def check_skill(skill_text: str, contract: Contract) -> list[str]:
    """Violations of the static contract: the ``SKILL.md`` must name every
    required section and every ``skill_must_mention`` marker verbatim, so a
    rename or a dropped guardrail fails before any session runs it."""
    violations: list[str] = []
    for section in contract.required_sections:
        if section not in skill_text:
            violations.append(f"skill text no longer names section {section!r}")
    for marker in contract.skill_must_mention:
        if marker not in skill_text:
            violations.append(f"skill text no longer mentions {marker!r}")
    return violations


# ---- whole-session replays (row 7.16) ---------------------------------------


def check_transcript(path: Path, contract: Contract) -> list[str]:
    """Apply a note contract to a runner transcript (``core.llm.transcript``):
    the ``run_end`` note goes through :func:`check_note`, and every command
    the session asked to run (allowed or refused) is swept for the banned
    strings, so a session that tried the merge command fails even when its
    note is silent about it."""
    from core.llm.transcript import commands_of, note_of

    violations = check_note(note_of(path), contract)
    for argv in commands_of(path):
        line = " ".join(argv).lower()
        for banned in contract.banned_strings:
            if banned.lower() in line:
                violations.append(f"banned string {banned!r} appears in a session command")
    return violations
