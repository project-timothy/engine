"""Who is asking: the read tools answer as one person under authority.toml.

Tim walkthrough 1 (2026-10-09): Grace Fellowship's authority.toml limited
Ruth, a missionary, to her own unit (``view:*@own``), but the read door
served the whole church to whoever held the link, and Ruth's question about
her own money came back with the Bakers' report beside hers. A ``Viewer``
puts the tenant's own rules in front of every row: a row reaches the person
only when ``evaluate`` lets them ``view`` its resource in its unit.

A unit is the missionary (an individual or a family); a person's unit is
the id authority.toml gives them, the slug of their name, so a row that
names a person ("Ruth Hollis") is in unit ``ruth-hollis``. A row that names
nobody (the church's own bill, the books) is in no unit, and only a ``view``
grant on every unit reaches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..authority import Policy, Request, evaluate
from ..engine.authority_gate import load_policy


class ViewerError(ValueError):
    """No such person to answer as, or no rules to answer under."""


def unit_of(name: str) -> str:
    """The unit a row's person is in: the id authority.toml gives a person
    (the slug of their name, as onboarding writes it). Empty for nobody."""
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").casefold()).strip("-")


@dataclass(frozen=True)
class Viewer:
    policy: Policy
    person: str

    def sees(self, resource: str, unit: str = "") -> bool:
        """Whether this person may view ``resource`` in ``unit``."""
        request = Request(self.person, "view", resource, scope=unit)
        return evaluate(self.policy, request).allowed


def viewer_for(slug: str, person: str, *, tenants_root: str | Path | None = None) -> Viewer:
    """The viewer for ``person`` (their id in authority.toml) in tenant
    ``slug``. Refuses a tenant with no authority.toml, an unknown name, and an
    agent: a door answers people."""
    loaded = load_policy(slug, tenants_root=Path(tenants_root) if tenants_root else None)
    if loaded is None:
        raise ViewerError(f"{slug} has no authority.toml, so there are no people to answer as")
    policy = loaded.policy
    if person in policy.agents:
        raise ViewerError(f"{person!r} is an agent; a door answers people")
    if person not in policy.people:
        known = ", ".join(sorted(policy.people)) or "nobody"
        raise ViewerError(f"{person!r} is not a person in {slug}'s authority.toml ({known})")
    return Viewer(policy, person)


__all__ = ["Viewer", "ViewerError", "unit_of", "viewer_for"]
