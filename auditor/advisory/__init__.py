"""The advisory voice: a CPA's eye on the books, not just a proofreader's.

Division of labor per the design (docs/auditor-design.md): deterministic
code COMPUTES every fact (facts.py); a model may only reason over and draft
from those computed facts (draft.py) — it makes no API calls, reads no
fresh data, and its whole output is prose in one report section. Advice
never creates checklist items, never sets severities, never touches a
book. The year-end-CPA appendix renders deterministically from the facts,
with or without a model in the loop.
"""

from .facts import compute_facts
from .render import render_advisory

__all__ = ["compute_facts", "render_advisory"]
