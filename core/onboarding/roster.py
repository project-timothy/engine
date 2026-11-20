"""People from one list, for a ministry that is already set up (chunk 11,
"ten units in a day"): a sending organization brings its new missionaries
on in one sitting.

One line a person, in the words setup asks for::

    Ivy Chen = missionary in Thailand, ivy.chen@example.org, looked after by Ann Lee and Rita Cole

Each line becomes what setup would have written for that person: a table in
authority.toml (their role, their own unit, the address their welcome link
goes to, the zone their place names, else the ministry's), a place in the
expense tree for anyone whose role files reports, and a place in the
scopes of each leader the line names, so their asks reach their own leader
on the first day. The doorkeeper's ``invite-all`` then mails each person
their welcome link.

The whole list is checked before a file is touched: one line that can't be
read refuses the list, naming that line. Someone already here (same id, or
the same address) is skipped, so the same list twice adds nobody twice.
Standard library only.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..authority import parse_policy
from ..engine.config import load_tenant, tenant_dir
from ..tools.viewer import unit_of
from . import (
    OnboardingError,
    _email_in,
    _files_expenses,
    _person_id,
    _place_zone,
    _role,
    filer_folders,
    person_table,
    persons_listed,
)

_LEADERS = re.compile(r",?\s*looked after by\s+(?P<names>.+)$", re.I)


@dataclass(frozen=True)
class Newcomer:
    pid: str
    name: str
    role: str
    email: str
    zone: str
    looked_after_by: tuple[str, ...]


@dataclass(frozen=True)
class Added:
    added: tuple[Newcomer, ...]
    already: tuple[str, ...]  # the names on the list who were here before it


def _names(said: str) -> list[str]:
    return [" ".join(n.split()) for n in re.split(r",|\band\b", said) if n.strip()]


def _read_line(line: str, roles: tuple[str, ...]) -> tuple[str, str, str, str, list[str]]:
    """(name, role, email, the role's words, the leaders named) from one line."""
    if "=" not in line:
        raise OnboardingError(f"{line.strip()!r} is not 'name = what they do'")
    rest, email = _email_in("people", line)
    name, what = rest.split("=", 1)
    leaders: list[str] = []
    what = what.strip()
    found = _LEADERS.search(what)
    if found:
        leaders = _names(found.group("names"))
        what = what[: found.start()]
    name = " ".join(name.split())
    if not name:
        raise OnboardingError(f"{line.strip()!r}: a person needs a name")
    try:
        role = _role("people", what, roles)
    except OnboardingError:
        raise OnboardingError(
            f"{line.strip()!r}: {what.strip(' ,;')!r} is not one of {', '.join(roles)}"
        ) from None
    return name, role, email, what, leaders


def add_people(
    tenant: str, said: str, *, tenants_root: str | Path | None = None, check: bool = False
) -> Added:
    """Bring on everyone ``said`` names, one per line; with ``check``, only
    say who would come on."""
    root = Path(tenants_root) if tenants_root else None
    directory = tenant_dir(tenant, tenants_root=root)
    cfg = load_tenant(tenant, tenants_root=root)
    authority = directory / "authority.toml"
    authority_text = authority.read_text(encoding="utf-8")
    data = tomllib.loads(authority_text)
    policy = parse_policy(data)
    agent_roles = {r for a in policy.agents.values() for r in a.roles}
    roles = tuple(r for r in policy.roles if r not in agent_roles)
    here: dict = data.get("people", {})
    taken = set(here)
    addresses = {
        str(e.get("email", "")).casefold(): pid
        for pid, e in here.items()
        if isinstance(e, dict) and e.get("email")
    }

    lines = [line for line in said.splitlines() if line.strip()]
    read = [(line, *_read_line(line, roles)) for line in lines]
    newcomers: list[Newcomer] = []
    already: list[str] = []
    new_ids: dict[str, str] = {}  # name folded -> id, for leaders in the same list
    for line, name, role, email, what, leaders in read:
        pid = unit_of(name)
        owner = addresses.get(email.casefold()) if email else None
        if pid in here or (owner is not None and owner == pid):
            already.append(name)
            continue
        if owner is not None:
            raise OnboardingError(f"{line.strip()!r}: {email} is already someone else's address")
        pid = _person_id(name, taken)
        if email:
            addresses[email.casefold()] = pid
        new_ids[name.casefold()] = pid
        zone = _place_zone(what) or cfg.identity.timezone
        newcomers.append(Newcomer(pid, name, role, email, zone, tuple(leaders)))

    # Each leader a line names: someone here or on the list, who files no
    # reports of their own (they could file and see a neighbor's).
    known = {pid: pid for pid in here} | {unit_of(n): pid for n, pid in new_ids.items()}
    roles_of = {pid: list(e.get("roles", [])) for pid, e in here.items() if isinstance(e, dict)}
    roles_of.update({n.pid: [n.role] for n in newcomers})
    gains: dict[str, list[str]] = {}
    for n in newcomers:
        for leader in n.looked_after_by:
            lid = known.get(unit_of(leader))
            if lid is None:
                raise OnboardingError(f"{n.name}: who is {leader!r}? Add them first")
            if any(_files_expenses(policy, r) for r in roles_of[lid] if r in policy.roles):
                raise OnboardingError(
                    f"{n.name}: {leader} files their own reports, so they can't look after "
                    "anyone else's"
                )
            gains.setdefault(lid, []).append(n.pid)

    if check or not newcomers:
        return Added(tuple(newcomers), tuple(already))

    tenant_file = directory / "tenant.toml"
    before = {authority: authority_text, tenant_file: tenant_file.read_text(encoding="utf-8")}
    try:
        text = authority_text
        for lid, charges in gains.items():
            if lid in here:
                text = _with_scopes(text, lid, [*here[lid].get("scopes", []), *charges])
        text += "".join(
            person_table(n.pid, n.name, n.role, [n.pid, *gains.get(n.pid, [])], n.email, n.zone)
            for n in newcomers
        )
        authority.write_text(text, encoding="utf-8")
        filers = [n.name for n in newcomers if _files_expenses(policy, n.role)]
        booked = [(p.name, p.role) for p in cfg.expenses.persons]
        booked += [(name, "employee") for name in filers if name not in {b[0] for b in booked}]
        tenant_file.write_text(persons_listed(before[tenant_file], booked), encoding="utf-8")
        # Prove every file still loads before calling it done.
        parse_policy(tomllib.loads(authority.read_text(encoding="utf-8")))
        load_tenant(tenant, tenants_root=root)
    except Exception:
        for path, original in before.items():
            path.write_text(original, encoding="utf-8")
        raise
    filer_folders(tenant, directory, filers)
    return Added(tuple(newcomers), tuple(already))


def _with_scopes(text: str, pid: str, scopes: list[str]) -> str:
    """authority.toml's text with ``pid``'s one scopes line made ``scopes``."""
    table = re.compile(rf"(?ms)^\[people\.{re.escape(pid)}\]\n.*?(?=^\[|\Z)")
    found = table.search(text)
    if found is None:
        raise OnboardingError(f"authority.toml: no table for {pid}")
    body, count = re.subn(
        r"(?m)^scopes = .*$",
        f"scopes = {json.dumps(list(dict.fromkeys(scopes)))}",
        found.group(0),
    )
    if count != 1:
        raise OnboardingError(f"authority.toml: expected one scopes line for {pid}")
    return text[: found.start()] + body + text[found.end() :]


def said_back(added: Added, *, check: bool = False) -> list[str]:
    """What the command says: one line each, then what comes next."""
    verb = "Would add" if check else "Added"
    out = []
    for n in added.added:
        line = f"{verb} {n.name} ({n.role}, {n.zone}"
        line += f", {n.email})" if n.email else ", no address: invite them by link)"
        if n.looked_after_by:
            line += f", looked after by {' and '.join(n.looked_after_by)}"
        out.append(line + ".")
    out += [f"{name} is already here." for name in added.already]
    if added.added and not check:
        out.append(
            "Next: `doorkeeper invite-all` mails each person with an address their welcome link."
        )
    return out


__all__ = ["Added", "Newcomer", "add_people", "said_back"]
