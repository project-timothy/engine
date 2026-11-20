"""Tim's two speeds, chosen by rule (Standalone Tim, shot 2, card V3).

Tim costs a ministry $5 a month, so everyday talk runs on the everyday
model (job type :data:`EVERYDAY`) and a hard question steps up to the
strong one (:data:`HARD`). Stepping up is code: the person never asks for
it and the model never decides it needs help. The rules, taken from the
Haiku trial of 2026-11-17, whose wrong answers were tax questions:

- **taxes**: her message is about taxes (a word or phrase in
  ``speeds.toml``), or the turn calls a tax tool;
- **statements**: the turn calls a bank-statement tool;
- **setup**: she has not finished her own setup (setup's own next screen
  is a step, ``core/tools/setup_page.py``);
- **again**: her message repeats her last one.

A rule that fires on a tool call mid-turn moves the rest of that turn to
the strong model; the steps already taken stand. Every call's job type is
on its ``llm_calls`` row, so the spend says which speed served it.

The cap holds over both (``[llm.budget].monthly_usd``, enforced on every
call by ``core.llm.policy``). Past its soft line (``soft_percent``, 80
unless the ministry's file says) Tim stops stepping up and says, once a
day, that he is near his month's limit.

A ministry that names ``talk`` and not ``talk_hard`` runs every turn on
``talk``. Both are named-only (``core.llm.policy.NAMED_ONLY``).

Either speed needs a tier that returns one plain reply per call. A tier on
an adapter that runs a whole agent session (:data:`AGENT_ADAPTERS`, the
owner's Claude seat) counts as not named: on 2026-11-18 the seat looked for
Tim's tools in its own session and told the person her books weren't
connected (issue #681).

Boundary classification (docs/boundary-rules.md): **code**. No model picks
a speed.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from ..engine.config import load_tenant
from ..llm.policy import named_on
from .meter import soft_line

EVERYDAY = "talk"
HARD = "talk_hard"
RULES_FILE = Path(__file__).with_name("speeds.toml")

TAXES, STATEMENTS, SETUP, AGAIN = "taxes", "statements", "setup", "again"
DOWN = "down"
"""Why a message stepped up when the everyday model gave no answer (:meth:`Speed.other`)."""

AGENT_ADAPTERS = frozenset({"claude_agent_sdk"})
"""Adapters that answer a call as a whole agent session with tools of its
own. They serve one-document jobs; Tim's conversation calls the engine's
tools itself, so neither speed runs on them."""


@cache
def rules() -> dict[str, Any]:
    """The word lists and tool lists, as the owner wrote them."""
    return tomllib.loads(RULES_FILE.read_text(encoding="utf-8"))


@cache
def _tax_words() -> re.Pattern[str]:
    phrases = sorted(rules()[TAXES]["words"], key=len, reverse=True)
    spelled = (r"\s+".join(re.escape(part) for part in p.split()) for p in phrases)
    return re.compile(r"(?<![\w-])(?:" + "|".join(spelled) + r")(?![\w-])", re.IGNORECASE)


def about_taxes(said: str) -> bool:
    """Her words name taxes."""
    return bool(_tax_words().search(said))


def for_tool(tool: str) -> str:
    """The rule a call to ``tool`` fires, or ""."""
    for rule in (TAXES, STATEMENTS):
        if tool in rules()[rule]["tools"]:
            return rule
    return ""


def _plain(said: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", said.casefold()).split())


def again(said: str, last: str) -> bool:
    """Her message is her last one again, case, spacing, and marks aside."""
    plain = _plain(said)
    return bool(plain) and plain == _plain(last)


def plain_tier(settings: Any, job: str) -> bool:
    """The ministry's file names a tier for ``job`` and that tier returns
    one plain reply per call (never an :data:`AGENT_ADAPTERS` session)."""
    if not named_on(settings, job):
        return False
    tier = settings.tiers.get(settings.jobs[job])
    return tier is not None and tier.adapter not in AGENT_ADAPTERS


def voice_on(settings: Any) -> bool:
    """Tim's own voice is on for the ministry: its file names a plain tier
    for everyday talk (card V1, issue #681). Where it's off, nothing a
    person sees changes (card T2: setup, the talk page, start_here, the
    welcome)."""
    return plain_tier(settings, EVERYDAY)


def voice_on_for(tenant: str, tenants_root: Any = None) -> bool:
    """``voice_on`` for a ministry, read from its file each time it's asked,
    so switching the voice on needs no restart; a file that can't be read
    leaves it off."""
    try:
        return voice_on(load_tenant(tenant, tenants_root=tenants_root).llm)
    except Exception:  # noqa: BLE001  a ministry file that can't be read: the voice stays off
        return False


def two_speeds(settings: Any) -> bool:
    """The ministry's file names a model for everyday talk and one for hard
    questions, both plain tiers. With the strong one alone, Tim has no voice."""
    return all(plain_tier(settings, job) for job in (EVERYDAY, HARD))


@dataclass
class Speed:
    """One message's speed. ``job`` is the job type the next call runs
    under; ``why`` the rule that stepped it up. ``near``: past the soft
    line, so it never steps up; ``tell``: and Tim hasn't said so today."""

    job: str = EVERYDAY
    why: str = ""
    can_step: bool = False
    near: bool = False
    tell: bool = False
    switched: bool = False

    def step_up(self, why: str) -> None:
        if why and self.can_step and self.job != HARD:
            self.job, self.why = HARD, why

    def called(self, tool: str) -> None:
        """A call the turn made: the rest of the turn steps up if it names
        a tax or statement tool."""
        self.step_up(for_tool(tool))

    def other(self) -> bool:
        """After a call that got no answer: the rest of the message runs on
        the other model, once (the owner's read of 2026-11-19: try again
        before any fallback line). Up to the strong one only where the
        message could step up at all (both tiers named, below the soft
        line); down to the everyday one always, since stepping up needed
        both. False once tried, or with no other model to try."""
        if self.switched or not self.can_step:
            return False
        self.switched = True
        if self.job == HARD:
            self.job = EVERYDAY
        else:
            self.job, self.why = HARD, self.why or DOWN
        return True


def pick(
    settings: Any,
    spent: Decimal,
    *,
    said: str | None,
    history: list[dict],
    today: str,
    in_setup: Callable[[], bool] | None = None,
) -> Speed:
    """The speed a message starts at. ``spent``: the ministry's model
    spend this month; ``said``: her words (None for a tap, which carries
    none); ``history``: her conversation, oldest first; ``today``: the UTC
    day, for the once-a-day line; ``in_setup``: whether she still has a
    setup step ahead, asked only when it could matter."""
    soft = soft_line(settings.budget)
    near = soft is not None and spent >= soft
    told = any(e.get("near") and str(e.get("at", ""))[:10] == today for e in history)
    speed = Speed(can_step=two_speeds(settings) and not near, near=near, tell=near and not told)
    if not speed.can_step:
        return speed
    if said is not None:
        speed.step_up(TAXES if about_taxes(said) else "")
        last = str(history[-1].get("said", "")) if history else ""
        speed.step_up(AGAIN if again(said, last) else "")
    if speed.job != HARD and in_setup is not None:
        try:
            unfinished = in_setup()
        except Exception:  # noqa: BLE001  a setup we can't read never costs her an answer
            unfinished = False
        speed.step_up(SETUP if unfinished else "")
    return speed


def at(instant: datetime) -> str:
    """The UTC day of ``instant``, as the conversation store stamps it."""
    return instant.astimezone(UTC).date().isoformat()


__all__ = [
    "EVERYDAY",
    "AGENT_ADAPTERS",
    "HARD",
    "RULES_FILE",
    "Speed",
    "about_taxes",
    "again",
    "at",
    "for_tool",
    "plain_tier",
    "two_speeds",
    "pick",
    "rules",
]
