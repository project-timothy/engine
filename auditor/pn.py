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

**The ``PYY_NNNN`` shape is one tenant's numbering scheme**, the first
tenant's, carried here as the default. A tenant that numbers projects
another way resolves through nicknames today; making the pattern itself a
tenant setting is a proposed change to what the auditor does, not taken here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

OVERHEAD = "OVERHEAD"
MULTI = "MULTI"

# A P-number embedded anywhere in a lower-cased string: an optional 'n' after
# the 'p' (PN00_0103), optional spaces, underscore-or-space between the
# 2-digit year and the 4-digit number (`p 25 _ 1017` included). `pn 1018` (four digits, no year) is
# deliberately NOT matched; that shape is a nickname.
_P_NUMBER = re.compile(r"p\s?n?\s?(\d{2})\s?_?\s?(\d{4})", re.IGNORECASE)
_DEFAULT_OVERHEAD = frozenset({"general/overhead", "general / overhead", "overhead"})
_MULTI_PREFIX = "multi"
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def find_pns(text: str | None) -> list[str]:
    """Every P-number in a free-text field, in order, canonical form."""
    if not text:
        return []
    return [f"P{y}_{n}" for y, n in _P_NUMBER.findall(text)]


def canonicalize(
    value: str | None,
    *,
    nicknames: Mapping[str, str] | None = None,
    overhead_tokens: Iterable[str] = (),
) -> str | None:
    """Resolve a project string to PYY_NNNN, OVERHEAD, MULTI, or None.

    Never raises. Order: overhead tokens, the multi prefix, an embedded
    P-number, then the nickname map."""
    if value is None:
        return None
    text = _norm(value)
    if not text:
        return None
    if text in _DEFAULT_OVERHEAD or text in {_norm(t) for t in overhead_tokens}:
        return OVERHEAD
    if text.startswith(_MULTI_PREFIX):
        return MULTI
    found = find_pns(text)
    if found:
        return found[0]
    for nick, pn in (nicknames or {}).items():
        if _norm(nick) == text:
            return str(pn)
    return None
