"""The tenant's voice check (docs/tenant-kit-design.md, section 2; issue #431).

Deterministic and model-free: a tenant's ``kit/voice.toml`` checked against
a piece of text, one hit per problem with the fix. The rules come from four
places in the voice file, plus the preset it names:

- **spelling**: ``en-US`` flags British forms, ``en-GB`` flags American ones.
  Explicit stems only, never a blanket ``-ise`` or ``-our`` rule ("promise",
  "exercise", "hour" and "four" are fine everywhere). ``en-GB`` leaves
  ``-ize`` alone, because Oxford British spelling uses it.
- **banned** words (whole words, case-insensitive) and **banned_patterns**
  (regular expressions, for shapes such as an em dash), from the tenant and
  from its preset. Both are fatal: the file says they never appear.
- **glossary**: ``"use this" = ["never this", ...]``. A warning with the term
  to use.
- **registers**: ``[registers.<name>] contractions = true|false``, applied
  only when a register is named. ``false`` makes any contraction fatal;
  ``true`` warns on a stiff phrase ("I am") that a person would contract.

Exit code: 0 clean, 1 warnings only, 2 any fatal.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PRESET_DIR = Path(__file__).resolve().parent / "presets"
PRESETS = ("plain-business", "ministry-conservative-christian")
"""The presets the engine ships. ``custom`` means the tenant wrote its own
and loads no preset."""

SPELLINGS = ("en-US", "en-GB")


class VoiceError(ValueError):
    """The voice file, or the request, says something the check cannot do."""


@dataclass(frozen=True)
class Hit:
    check: str
    severity: str  # fatal | warn
    message: str
    excerpt: str
    line: int


# ---- spelling -------------------------------------------------------------------

_IZE_STEMS = (
    "organis|recognis|realis|prioritis|summaris|minimis|maximis|optimis|"
    "standardis|customis|authoris|categoris|finalis|utilis|normalis|"
    "capitalis|visualis|specialis|apologis|criticis|centralis|emphasis(?=e)|"
    "memoris|monetis|personalis|stabilis|synchronis|analys(?=e[sd]?\\b|ing)"
)
_DOUBLE_L = "travel|cancel|model|label|fuel|signal|level|total|channel|counsel|marshal"
_OR_STEMS = "col|behavi|fav|hon|lab|neighb|flav|hum|harb|rum|vap|rig|endeav|savi"
_OR_SUFFIX = "(s|ed|ing|ite|ites|ful|able|hood|ly)?"

BRITISH_FORMS = [
    (rf"\b({_OR_STEMS})our{_OR_SUFFIX}\b", "-our -> -or"),
    (r"\b(cent|met|lit|theat|fib|calib|somb)re(s|d)?\b", "-re -> -er"),
    (rf"\b(?:{_IZE_STEMS})(e|ed|es|ing|ation|ations|er|ers)\b", "-ise -> -ize"),
    (rf"\b({_DOUBLE_L})l(ed|ing|er|ers)\b", "double l -> single l"),
    (r"\b(licence|defence|offence|pretence)s?\b", "-ence -> -ense"),
    (
        r"\b(catalogue|programme|judgement|cheque|grey|tyre|kerb|plough|manoeuvre|"
        r"aeroplane|mould|artefact|sceptic|aluminium|whilst|amongst)(s|d|al)?\b",
        "use the American form",
    ),
]
"""What an ``en-US`` tenant never writes (ported from the first tenant's own
checker, which proved the stems against false positives)."""

AMERICAN_FORMS = [
    (rf"\b({_OR_STEMS})or{_OR_SUFFIX}\b", "-or -> -our"),
    (r"\b(cent|theat|fib|lit)er(s|ed)?\b", "-er -> -re"),
    (rf"\b({_DOUBLE_L})(ed|ing|er|ers)\b", "single l -> double l"),
    (r"\b(defense|offense|pretense)s?\b", "-ense -> -ence"),
    (r"\b(gray|aluminum|plow|maneuver|skeptic|artifact|airplane)(s|ed|al)?\b", "British form"),
]
"""What an ``en-GB`` tenant never writes. ``-ize`` is absent on purpose."""


# ---- registers ---------------------------------------------------------------------

_CONTRACTION = re.compile(
    r"\b(\w+n't|\w+'re|\w+'ve|\w+'ll|I'm|\w+'d|"
    r"(?:it|that|there|here|what|who|he|she|let|where|how)'s)\b",
    re.IGNORECASE,
)
_STIFF = {
    "I am": "I'm",
    "it is": "it's",
    "do not": "don't",
    "does not": "doesn't",
    "is not": "isn't",
    "are not": "aren't",
    "we are": "we're",
    "you are": "you're",
    "they are": "they're",
    "we will": "we'll",
    "I will": "I'll",
    "will not": "won't",
    "cannot": "can't",
    "that is": "that's",
    "there is": "there's",
}


# ---- the voice file ----------------------------------------------------------------


@cache
def load_preset(name: str) -> dict:
    if name not in PRESETS:
        raise VoiceError(f"preset {name!r} is not one of {', '.join(PRESETS)} or custom")
    with (PRESET_DIR / f"{name}.toml").open("rb") as handle:
        return tomllib.load(handle)


def validate_voice(data: dict) -> None:
    """Every rule in a voice file must be one the check can apply."""
    if data.get("spelling") not in SPELLINGS:
        raise VoiceError(f"spelling = {data.get('spelling')!r}: use one of {', '.join(SPELLINGS)}")
    preset = data.get("preset")
    if preset != "custom":
        if not isinstance(preset, str) or not preset:
            raise VoiceError("no preset: name one, or write your own and call it custom")
        load_preset(preset)
    for key in ("banned", "banned_patterns"):
        values = data.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise VoiceError(f"{key} must be a list of strings")
    for pattern in data.get("banned_patterns", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise VoiceError(f"banned_patterns {pattern!r} does not compile: {exc}") from exc
    glossary = data.get("glossary", {})
    if not isinstance(glossary, dict) or not all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in glossary.values()
    ):
        raise VoiceError('glossary entries are "use this" = ["never this", ...]')
    for name, table in data.get("registers", {}).items():
        if not isinstance(table, dict) or not isinstance(table.get("contractions"), bool):
            raise VoiceError(f"[registers.{name}] needs contractions = true or false")


# ---- the check ---------------------------------------------------------------------


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _excerpt(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start : line_end if line_end != -1 else len(text)].strip()
    return line if len(line) <= 100 else text[max(start - 40, 0) : end + 40].strip()


def _words(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


def check_text(text: str, voice: dict, *, register: str | None = None) -> list[Hit]:
    """Every hit in ``text`` under ``voice`` (a parsed kit/voice.toml), in
    order of appearance."""
    validate_voice(voice)
    text = text.replace("’", "'")
    preset = {} if voice.get("preset") == "custom" else load_preset(voice["preset"])
    hits: list[tuple[int, Hit]] = []

    def add(start: int, end: int, check: str, severity: str, message: str) -> None:
        hit = Hit(check, severity, message, _excerpt(text, start, end), _line_of(text, start))
        hits.append((start, hit))

    forms = BRITISH_FORMS if voice["spelling"] == "en-US" else AMERICAN_FORMS
    for pattern, fix in forms:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            add(
                m.start(),
                m.end(),
                "spelling",
                "fatal",
                f'"{m.group(0)}": {fix} ({voice["spelling"]})',
            )

    banned = [*preset.get("banned", []), *voice.get("banned", [])]
    for phrase in dict.fromkeys(banned):
        for m in _words(phrase.replace("’", "'")).finditer(text):
            add(m.start(), m.end(), "banned", "fatal", f'"{m.group(0)}" never appears')
    patterns = [*preset.get("banned_patterns", []), *voice.get("banned_patterns", [])]
    for pattern in dict.fromkeys(patterns):
        for m in re.finditer(pattern, text):
            add(m.start(), m.end(), "banned-pattern", "fatal", f"matches banned shape {pattern!r}")

    for use, avoid in voice.get("glossary", {}).items():
        for term in avoid:
            for m in _words(term).finditer(text):
                add(m.start(), m.end(), "glossary", "warn", f'"{m.group(0)}": say "{use}"')

    if register is not None:
        registers = voice.get("registers", {})
        if register not in registers:
            raise VoiceError(
                f"register {register!r} is not in voice.toml ({', '.join(registers) or 'none'})"
            )
        if registers[register]["contractions"]:
            for stiff, contracted in _STIFF.items():
                for m in _words(stiff).finditer(text):
                    message = f'"{m.group(0)}" -> "{contracted}" ({register} reads as a person)'
                    add(m.start(), m.end(), "stiff", "warn", message)
        else:
            for m in _CONTRACTION.finditer(text):
                add(
                    m.start(),
                    m.end(),
                    "contraction",
                    "fatal",
                    f'"{m.group(0)}": no contractions ({register})',
                )

    return [hit for _start, hit in sorted(hits, key=lambda pair: pair[0])]


def exit_code(hits: list[Hit]) -> int:
    if any(h.severity == "fatal" for h in hits):
        return 2
    return 1 if hits else 0


__all__ = [
    "AMERICAN_FORMS",
    "BRITISH_FORMS",
    "PRESETS",
    "SPELLINGS",
    "Hit",
    "VoiceError",
    "check_text",
    "exit_code",
    "load_preset",
    "validate_voice",
]
