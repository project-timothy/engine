"""Agency packs: what a sending agency sets for the ministry set up from it
(board item t-agency-pack; the owner's decisions of 2026-10-15: "a backbone
of deterministic code ... flexibility in most values and in logic flow").

A pack is a folder:

- ``pack.toml``: the agency's name, the shape it sets up (an organization:
  an agency runs as itself, never on top of a church, 2026-10-15), the
  categories it adds to the starter list, and every number it sets, each
  with the words it came from (``[[number]]``: ``at``, ``value``, ``says``,
  ``source``).
- ``authority.toml``: its roles, safeguards and routes, in the authority
  file's own format (core/authority), with no people; setup adds them.
- ``rules/``: its handbook layers (core/policy), beside the copies they
  were read from, which the morning watch keeps reading.

Numbers and the shape of the organization vary freely from pack to pack.
What the engine does with them never does: a pack only fills in what the
engine already holds. ``check`` keeps a pack honest: every amount in its
authority file is cited, and every citation still reads there; its rules
load against its own roles and categories; every quote, a rule's or a
number's, still reads in the copy of its document beside it.

Nothing here reads a ledger or a tenant: setup (core/onboarding) lays a
pack down as a new ministry's files.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..authority import AuthorityError, parse_policy
from ..engine.init import load_ministry_categories
from ..policy import RULES_DIR, PolicyError, Source, find_quote, load_rulebook, parse_source
from ..policy.copies import CopyUnreadable, read_copy

PACK_FILE = "pack.toml"
AUTHORITY_FILE = "authority.toml"
SHAPES = ("nonprofit-organization",)
"""What a pack sets up: an organization. A small church and a solo
missionary keep the engine's defaults; the three never stack."""

SAFEGUARD_AMOUNTS = (
    "self_approval_limit",
    "distinct_payer_above",
    "second_approver_above",
    "yes_by_word_up_to",
)

_PACK_KEYS = frozenset({"name", "shape", "categories", "number"})
_NUMBER_KEYS = frozenset({"at", "value", "says", "source"})


class PackError(ValueError):
    """A pack says something the engine cannot hold."""


@dataclass(frozen=True)
class Number:
    """One number a pack sets: where in its authority file (``at``:
    ``safeguards.<key>``, ``roles.<role>`` for the limit on its grants, or
    ``routing.<resource>`` for a route's ``above``), the amount, Tim's words
    for it, and the words it came from."""

    at: str
    value: Decimal
    says: str
    source: Source


@dataclass(frozen=True)
class Pack:
    name: str
    shape: str
    directory: Path
    numbers: tuple[Number, ...]
    categories: tuple[str, ...]
    authority: dict
    roles: tuple[str, ...]
    """The roles a person can hold: the pack's, minus its agents' own."""

    @property
    def authority_text(self) -> str:
        return (self.directory / AUTHORITY_FILE).read_text(encoding="utf-8")

    @property
    def kinds(self) -> list[str]:
        """The categories its ministry starts with: the starter list and the pack's."""
        return list(dict.fromkeys([*load_ministry_categories(), *self.categories]))


def _amount(where: str, value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PackError(f"{where}: value must be an amount, not {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise PackError(f"{where}: value must be an amount over zero, not {value!r}")
    return amount


def _number(index: int, table: object) -> Number:
    where = f"{PACK_FILE} number {index + 1}"
    if not isinstance(table, dict):
        raise PackError(f"{where}: a number is a table")
    extra = sorted(set(table) - _NUMBER_KEYS)
    if extra:
        raise PackError(f"{where}: unknown field {', '.join(extra)}")
    at = str(table.get("at") or "")
    if not at.startswith(("safeguards.", "roles.", "routing.")):
        raise PackError(f"{where}: at names safeguards.<key>, roles.<role> or routing.<resource>")
    says = " ".join(str(table.get("says") or "").split())
    if not says:
        raise PackError(f"{where} ({at}): says is the words Tim uses for it")
    source = table.get("source")
    try:
        cited = parse_source(f"{where} ({at})", "", source)
    except PolicyError as exc:
        raise PackError(str(exc)) from exc
    if not cited.document:
        raise PackError(f"{where} ({at}): source needs the document it came from")
    return Number(at, _amount(f"{where} ({at})", table.get("value")), says, cited)


def load_pack(directory: str | Path) -> Pack:
    """The pack in ``directory``. Raises ``PackError`` naming the first thing
    it cannot hold; ``check`` says the rest."""
    folder = Path(directory)
    path = folder / PACK_FILE
    if not path.is_file():
        raise PackError(f"{folder}: no {PACK_FILE}, so this is not a pack")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        authority = tomllib.loads((folder / AUTHORITY_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackError(f"{folder}: a pack carries its {AUTHORITY_FILE}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PackError(f"{folder}: not valid TOML ({exc})") from exc
    extra = sorted(set(data) - _PACK_KEYS)
    if extra:
        raise PackError(f"{PACK_FILE}: unknown field {', '.join(extra)}")
    name = str(data.get("name") or "").strip()
    if not name:
        raise PackError(f"{PACK_FILE}: name the agency")
    shape = str(data.get("shape") or "")
    if shape not in SHAPES:
        raise PackError(
            f"{PACK_FILE}: shape = {shape!r}; a pack sets up an organization ({', '.join(SHAPES)})"
        )
    categories = data.get("categories", [])
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        raise PackError(f"{PACK_FILE}: categories is a list of names")
    if authority.get("people"):
        raise PackError(f"{AUTHORITY_FILE}: a pack names no people; setup adds them")
    try:
        policy = parse_policy(authority)
    except AuthorityError as exc:
        raise PackError(f"{AUTHORITY_FILE}: {exc}") from exc
    agent_roles = {r for a in policy.agents.values() for r in a.roles}
    return Pack(
        name=name,
        shape=shape,
        directory=folder,
        numbers=tuple(_number(i, t) for i, t in enumerate(data.get("number", []))),
        categories=tuple(categories),
        authority=authority,
        roles=tuple(r for r in policy.roles if r not in agent_roles),
    )


def amounts(authority: dict) -> list[tuple[str, Decimal]]:
    """Every amount over zero an authority file sets, by where it sits."""
    found: list[tuple[str, Decimal]] = []
    safeguards = authority.get("safeguards", {})
    for key in SAFEGUARD_AMOUNTS:
        value = Decimal(str(safeguards.get(key, 0)))
        if value > 0:
            found.append((f"safeguards.{key}", value))
    policy = parse_policy(authority)
    for role, held in policy.roles.items():
        for limit in dict.fromkeys(g.limit for g in held.grants if g.limit is not None):
            found.append((f"roles.{role}", limit))
    for route in policy.routing.routes:
        if route.above > 0:
            found.append((f"routing.{route.resource}", route.above))
    return found


def _copies(pack: Pack, problems: list[str]) -> dict[str, list[str]]:
    """Each document's words, page by page, from the copies its rules name."""
    pages: dict[str, list[str]] = {}
    try:
        book = load_rulebook(pack.directory, roles=set(pack.roles), people=set(), kinds=pack.kinds)
    except PolicyError as exc:
        problems.append(f"{RULES_DIR}/: {exc}")
        return pages
    for layer in book.layers if book else ():
        if layer.copy is None:
            continue
        try:
            sha, words = read_copy(layer, layer.copy)
        except CopyUnreadable as exc:
            problems.append(str(exc))
            continue
        if layer.copy.sha and sha != layer.copy.sha:
            problems.append(f"{layer.file}: its copy is not the one the rules were read from")
        pages[layer.document] = words
        for rule in layer.rules:
            if find_quote(rule.source.quote, words) is None:
                problems.append(
                    f"{layer.file}: rule {rule.id}'s words are not in its copy of {layer.document}"
                )
    return pages


def check(pack: Pack) -> list[str]:
    """What keeps ``pack`` from setting up a ministry, each said plainly;
    empty when it is ready."""
    problems: list[str] = []
    cited = {(n.at, n.value) for n in pack.numbers}
    present = amounts(pack.authority)
    for at, value in present:
        if (at, value) not in cited:
            problems.append(f"{AUTHORITY_FILE}: {at} sets {value}, which no number cites")
    for n in pack.numbers:
        if (n.at, n.value) not in present:
            problems.append(f"{PACK_FILE}: {n.at} is cited at {n.value}, which is not what it sets")
    pages = _copies(pack, problems)
    for n in pack.numbers:
        words = pages.get(n.source.document)
        if words is not None and find_quote(n.source.quote, words) is None:
            problems.append(
                f"{PACK_FILE}: the words for {n.at} are not in the copy of {n.source.document}"
            )
    return problems


__all__ = ["Number", "Pack", "PackError", "amounts", "check", "load_pack"]
