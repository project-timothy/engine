"""Project-number canonicalization: one PN, four written forms.

Ported 2026-09-04 from the retired bookkeeper-auditor's ``projects.py``
(its regex and tests, adapted). A tenant's records write the same project
several ways across AP rows, expense lines, timesheet lines, and the
project registry: the canonical ``PYY_NNNN``, the house gl_account spelling
``PN00_0103``, a P-number with a trailing or parenthetical nickname, a
hand-typed ``p26 2034``, and bare nicknames that carry no number at all.
The registry's ``pn`` column is the canonical form, so everything resolves
to it:

- a ``PYY_NNNN`` string, a real project;
- the sentinel ``OVERHEAD``, a non-project bucket;
- the sentinel ``MULTI``, a consolidated multi-project row (legacy shape);
- ``None``: unresolvable; the caller decides what that means.

Nicknames are tenant data (``[auditor.projects].nicknames`` in tenant.toml),
never code: keys are matched case-insensitively with whitespace collapsed.

**The numbering scheme is tenant data** (#340): ``[books.cost_object]``
gives a pattern whose groups make the code and a canonical format
(``"P{0}_{1}"`` for the first tenant). The auditor reads that table itself
(it imports nothing from core) and hands a :class:`CodeFormat` here. No
pattern resolves no text as a project code; nicknames still resolve.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

OVERHEAD = "OVERHEAD"
MULTI = "MULTI"

_DEFAULT_OVERHEAD = frozenset({"general/overhead", "general / overhead", "overhead"})
_MULTI_PREFIX = "multi"
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class CodeFormat:
    """A tenant's project-code scheme: ``pattern``'s groups, formatted by
    ``canonical``. The empty format finds nothing."""

    pattern: str = ""
    canonical: str = ""

    def find(self, text: str) -> list[str]:
        if not self.pattern:
            return []
        return [self.canonical.format(*m.groups()) for m in re.finditer(self.pattern, text)]


NO_FORMAT = CodeFormat()


def _norm(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def find_pns(text: str | None, code: CodeFormat = NO_FORMAT) -> list[str]:
    """Every project code in a free-text field, in order, canonical form."""
    if not text:
        return []
    return code.find(text)


def canonicalize(
    value: str | None,
    *,
    nicknames: Mapping[str, str] | None = None,
    overhead_tokens: Iterable[str] = (),
    code: CodeFormat = NO_FORMAT,
) -> str | None:
    """Resolve a project string to its canonical code, OVERHEAD, MULTI, or None.

    Never raises. Order: overhead tokens, the multi prefix, an embedded
    project code, then the nickname map."""
    if value is None:
        return None
    text = _norm(value)
    if not text:
        return None
    if text in _DEFAULT_OVERHEAD or text in {_norm(t) for t in overhead_tokens}:
        return OVERHEAD
    if text.startswith(_MULTI_PREFIX):
        return MULTI
    found = find_pns(text, code)
    if found:
        return found[0]
    for nick, pn in (nicknames or {}).items():
        if _norm(nick) == text:
            return str(pn)
    return None
