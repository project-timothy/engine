"""The one redactor (phase 7 row 7.22).

The W-9 lane's invariant is the model: a TIN is read in memory, checked, and
discarded, so it never reaches a card, an event, or a log. Row 7.22 gives a
container somewhere to keep provider keys, which makes the same question
urgent for TOKENS: a key that arrives inside a document, an adapter's error
body, or a job's own summary must not become a permanent line in a git-backed
ledger that is pushed to a backup remote every night, where it outlives every
rotation.

This module is that redactor, and there is exactly one. ``core.llm.transcript``
had it first (for session transcripts) and now imports it from here; the
runner applies it to every ``JobOutput`` before anything is stored. It lives
at the top of ``core/`` rather than under ``core/engine/`` or ``core/llm/``
because both of those import it and neither may import the other.

## The four rules, in order

1. **The VALUE of a declared secret.** ``tenant.toml`` names the environment
   variable; the caller hands over ``(name, value)`` pairs and the value is
   replaced with ``<redacted:NAME>``. This is the only airtight rule, because
   it is the only one that does not guess.
2. **Named token families.** ``sk-``, GitHub, Slack, AWS, Google, JWT, and
   ``Bearer``/``Basic`` credentials. Zero false positives: no identifier this
   engine writes begins with any of those.
3. **TIN shapes.** EIN ``dd-ddddddd`` and SSN ``ddd-dd-dddd``, unchanged.
4. **A whole value that is one padded base64 blob.** Deliberately the
   narrowest generic rule that exists.

## What is NOT here, and why

There is no entropy rule and no "opaque 32-character run" rule. Both were
measured before this module was written, over the identifiers the engine
actually writes: ``Invoice_2026-09-16_Acme_Fabrication_PO`` scores 4.37 bits
per character and ``COGS-Project_Expense-PN00_0412_Subaccount`` 4.55, while
random 32-character tokens ran 4.33 to 4.90. The distributions overlap, so
any threshold either shreds file names and account names or misses tokens.
Rule 4 survives that test because base64 padding and ``+`` are characters no
identifier in this engine contains.

Hex is never redacted at any length. sha16 content keys, sha256 digests,
``stmt:`` statement-line ids and run keys are all hex, and redacting one would
break a card's memory, a file's provenance, or a run key.

Rule 4 also applies to a WHOLE string only, never to a run inside a sentence:
a sentence is not a token, and scanning prose for opaque runs is what turns a
redactor into a shredder. Rules 1 to 3 cover prose.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any

KEY_MARK = "<redacted:key>"
TIN_MARK = "<redacted:tin>"
TOKEN_MARK = "<redacted:token>"

_KEY_PATTERNS = (
    # OpenAI and Anthropic style, and anything else that copied the prefix.
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    # GitHub: personal, oauth, user, server, refresh; and fine-grained.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    # Slack bot, user, app, refresh and legacy tokens.
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{8,}"),
    # AWS access key id, Google API key.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    # A JWT: three base64url segments. Bearer and Basic credentials.
    re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
)
_TIN_PATTERNS = (
    re.compile(r"\b\d{2}-\d{7}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
)

# Rule 4. A whole value, 32 characters or more, drawn from the base64
# alphabet, carrying at least one character no identifier here ever does
# (``+`` or the ``=`` padding), and not pure hex.
_BLOB = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")
_HEX = re.compile(r"(?i)[0-9a-f]+")


def env_values(names: Iterable[str]) -> list[tuple[str, str]]:
    """``(name, value)`` for every named variable that is set and non-empty,
    longest value first so a value that contains another is replaced whole."""
    pairs = [(n, os.environ[n]) for n in names if os.environ.get(n)]
    return sorted(pairs, key=lambda p: -len(p[1]))


def _is_blob(text: str) -> bool:
    if not _BLOB.fullmatch(text):
        return False
    if _HEX.fullmatch(text):
        return False
    return "+" in text or text.endswith("=")


def redact_text(text: str, values: Iterable[tuple[str, str]] = ()) -> str:
    """Rules 1 to 4 over one string. Idempotent: running it twice changes
    nothing, so a replayed payload is stable."""
    for name, value in values:
        text = text.replace(value, f"<redacted:{name}>")
    for pattern in _KEY_PATTERNS:
        text = pattern.sub(KEY_MARK, text)
    for pattern in _TIN_PATTERNS:
        text = pattern.sub(TIN_MARK, text)
    if _is_blob(text):
        return TOKEN_MARK
    return text


def redact(obj: Any, *, env_values: Iterable[tuple[str, str]] = ()) -> Any:
    """Redact every string inside a JSON-shaped object, recursively. Numbers,
    booleans, and ``None`` pass through untouched. Dictionary KEYS are left
    alone: they are field names, and in this engine several of them are
    idempotency keys."""
    values = list(env_values)

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return redact_text(node, values)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(obj)


__all__ = ["KEY_MARK", "TIN_MARK", "TOKEN_MARK", "env_values", "redact", "redact_text"]
