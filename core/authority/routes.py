"""Routes, as ``authority.toml`` says them (docs/tenant-kit-design.md,
section 3, "Routing: one document, on a clock"; #436).

``[routing]`` holds the clock and the escalation settings; each
``[[routing.route]]`` gives a resource above an amount its ordered steps.
A step names a role, never a person, so a route survives someone leaving.
Defaults are gentle: no backup and no upward routing unless turned on.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .errors import AuthorityError

NEEDS = ("one", "two", "all")
"""How many approvers of the step's role it takes: one of them, two
distinct people, or every person holding the role."""


@dataclass(frozen=True)
class Step:
    role: str = ""  # "" = anyone the evaluator allows
    need: str = "one"


@dataclass(frozen=True)
class Route:
    resource: str = "*"
    above: Decimal = Decimal("0")
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Routing:
    """The clock: a step waits against the document's deadline, else
    ``window_days`` from when it opened. Reminders keep the window's shape,
    anchored to that date: the first ``window_days - first_reminder_days``
    before it, the second ``window_days - second_reminder_days`` before it,
    the backup (when turned on) ``window_days - backup_after_days`` before
    it. Upward routing (organization shape only) comes on the date itself."""

    window_days: int = 5
    first_reminder_days: int = 2
    second_reminder_days: int = 4
    backup: bool = False
    backup_after_days: int = 5
    upward: bool = False
    upward_role: str = ""
    owner: str = ""
    routes: tuple[Route, ...] = ()


def _days(table: dict, key: str, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AuthorityError(f"[routing] {key} is a whole number of days")
    return value


def _flag(table: dict, key: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise AuthorityError(f"[routing] {key} is true or false")
    return value


def _route(i: int, table: dict, roles: set[str], resources: tuple[str, ...]) -> Route:
    where = f"[[routing.route]] #{i + 1}"
    resource = str(table.get("resource", "*"))
    if resource != "*" and resource not in resources:
        raise AuthorityError(f"{where}: unknown resource {resource!r}")
    try:
        above = Decimal(str(table.get("above", 0)))
    except InvalidOperation as exc:
        raise AuthorityError(f"{where}: above must be an amount") from exc
    steps = []
    for raw in table.get("steps", []):
        if not isinstance(raw, dict):
            raise AuthorityError(f"{where}: each step is {{ role = ..., need = ... }}")
        step = Step(str(raw.get("role", "")), str(raw.get("need", "one")))
        if step.role and step.role not in roles:
            raise AuthorityError(f"{where}: role {step.role!r} is not in [roles]")
        if step.need not in NEEDS:
            raise AuthorityError(f"{where}: need must be one of {', '.join(NEEDS)}")
        if step.need == "all" and not step.role:
            raise AuthorityError(f"{where}: need = 'all' names a role")
        steps.append(step)
    if not steps:
        raise AuthorityError(f"{where}: a route has at least one step")
    return Route(resource, above, tuple(steps))


def parse_routing(
    data: dict, *, roles: set[str], people: set[str], resources: tuple[str, ...]
) -> Routing:
    if not isinstance(data, dict):
        raise AuthorityError("[routing] is a table")
    upward_role = str(data.get("upward_role", ""))
    if upward_role and upward_role not in roles:
        raise AuthorityError(f"[routing] upward_role {upward_role!r} is not in [roles]")
    if _flag(data, "upward") and not upward_role:
        raise AuthorityError("[routing] upward = true names an upward_role")
    owner = str(data.get("owner", ""))
    if owner and owner not in people:
        raise AuthorityError(f"[routing] owner {owner!r} is not in [people]")
    routing = Routing(
        window_days=_days(data, "window_days", 5),
        first_reminder_days=_days(data, "first_reminder_days", 2),
        second_reminder_days=_days(data, "second_reminder_days", 4),
        backup=_flag(data, "backup"),
        backup_after_days=_days(data, "backup_after_days", 5),
        upward=_flag(data, "upward"),
        upward_role=upward_role,
        owner=owner,
        routes=tuple(_route(i, t, roles, resources) for i, t in enumerate(data.get("route", []))),
    )
    for key in ("first_reminder_days", "second_reminder_days", "backup_after_days"):
        if getattr(routing, key) > routing.window_days:
            raise AuthorityError(f"[routing] {key} falls past window_days")
    return routing


__all__ = ["NEEDS", "Route", "Routing", "Step", "parse_routing"]
