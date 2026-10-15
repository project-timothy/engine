"""Policy rules: a ministry's manuals as rules the engine holds (board item
t-policy-rules; the owner's decisions of 2026-10-14).

One rule says who it covers, what kind of spending, up to how much (an item,
a month, a year), what it needs from an amount on (an approver, approval
before buying, the person's own Face ID, a receipt, a receipt that is not a
bank statement, a budget line), the words Tim says about it, and the words
it came from. A model may draft a rule from a handbook; only a person's yes
puts one to work, and code applies it (invariant 2).

Rules come in layers, ``handbook > executive > region > tenant > person``,
one file per document under the tenant's ``rules/`` folder. Every layer's
rules apply at once, so the strictest wins and a lower layer can only add.
The one thing a lower layer could still say is a rule that LOOKS looser than
one above it (a higher cap, an approver only at a higher amount); that is
refused at load with both rules and the source named, so nobody reads a
region's file and believes it.

Nothing here reads a ledger, a card, or a tenant's config: callers pass the
ministry's roles, people and categories, and get rules back.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

LAYERS = ("handbook", "executive", "region", "tenant", "person")
"""Highest first. A layer may only make the ones above it stricter."""

CAPS = ("item_up_to", "month_up_to", "year_up_to")
NEEDS = ("approver", "before", "face_id", "receipt", "receipt_not_bank", "budgeted")
"""What a rule may ask for from its ``over`` amount on. ``approver`` names a
role (or person) whose OK it needs; the rest are yes/no, and only "yes" says
anything, so a lower layer cannot switch one off by writing "no"."""

RULES_DIR = "rules"

_RULE_KEYS = frozenset({"id", "who", "kinds", "over", "says", "needs", "source", *CAPS})
_SOURCE_KEYS = frozenset({"document", "section", "page", "quote"})
_LAYER_KEYS = frozenset({"layer", "document", "person", "rule"})


class PolicyError(ValueError):
    """A rules file says something the engine cannot hold."""


@dataclass(frozen=True)
class Source:
    document: str
    quote: str
    section: str = ""
    page: int | None = None


@dataclass(frozen=True)
class Needs:
    approver: str = ""
    before: bool = False
    face_id: bool = False
    receipt: bool = False
    receipt_not_bank: bool = False
    budgeted: bool = False

    def asks(self) -> list[str]:
        """What these needs ask for, one key per need ("approver:team-leader")."""
        said = [f"approver:{self.approver}"] if self.approver else []
        return said + [k for k in NEEDS[1:] if getattr(self, k)]


@dataclass(frozen=True)
class Rule:
    id: str
    layer: str
    says: str
    source: Source
    who: tuple[str, ...] = ("*",)
    kinds: tuple[str, ...] = ("*",)
    over: Decimal = Decimal("0")
    item_up_to: Decimal | None = None
    month_up_to: Decimal | None = None
    year_up_to: Decimal | None = None
    needs: Needs = field(default_factory=Needs)

    @property
    def sha(self) -> str:
        """The rule's fingerprint: every word and amount in it, so a yes
        given to one wording never covers another."""
        body = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def cite(self) -> str:
        """Where it came from, the way a person names a page."""
        parts = [self.source.document, self.source.section]
        if self.source.page is not None:
            parts.append(f"p. {self.source.page}")
        return ", ".join(p for p in parts if p)

    def covers(self, person: str, roles: tuple[str, ...], kind: str) -> bool:
        who = "*" in self.who or person in self.who or bool(set(roles) & set(self.who))
        what = "*" in self.kinds or kind.casefold() in {k.casefold() for k in self.kinds}
        return who and what


@dataclass(frozen=True)
class Layer:
    layer: str
    document: str
    rules: tuple[Rule, ...]
    person: str = ""
    file: str = ""


@dataclass(frozen=True)
class Rulebook:
    layers: tuple[Layer, ...]

    @property
    def rules(self) -> list[Rule]:
        return [r for layer in self.layers for r in layer.rules]

    def applicable(self, *, person: str, roles: tuple[str, ...], kind: str) -> list[Rule]:
        """Every rule that covers this person spending on this kind, highest
        layer first. All of them apply."""
        return [r for r in self.rules if r.covers(person, roles, kind)]


# ---- parsing --------------------------------------------------------------------


def _amount(where: str, key: str, value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PolicyError(f"{where}: {key} must be an amount, not {value!r}") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise PolicyError(f"{where}: {key} must be dollars and cents, not {value!r}")
    return amount


def _names(where: str, key: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ("*",)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where}: {key} is a list of names")
    return tuple(value)


def _unknown(where: str, table: dict, allowed: frozenset[str]) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise PolicyError(
            f"{where}: unknown field {', '.join(extra)} ({', '.join(sorted(allowed))})"
        )


def _source(where: str, document: str, table: object) -> Source:
    if not isinstance(table, dict) or not str(table.get("quote", "")).strip():
        raise PolicyError(f"{where}: source needs the quote it came from")
    _unknown(f"{where} source", table, _SOURCE_KEYS)
    page = table.get("page")
    if page is not None and (not isinstance(page, int) or page < 1):
        raise PolicyError(f"{where}: source page is a page number")
    return Source(
        document=str(table.get("document") or document),
        quote=str(table["quote"]).strip(),
        section=str(table.get("section", "")),
        page=page,
    )


def _needs(where: str, table: object) -> Needs:
    if table is None:
        return Needs()
    if not isinstance(table, dict):
        raise PolicyError(f"{where}: needs is a table")
    _unknown(f"{where} needs", table, frozenset(NEEDS))
    for key in NEEDS[1:]:
        if key in table and not isinstance(table[key], bool):
            raise PolicyError(f"{where}: needs {key} is true or false")
    approver = table.get("approver", "")
    if not isinstance(approver, str):
        raise PolicyError(f"{where}: needs approver names one role")
    return Needs(approver=approver, **{k: bool(table.get(k, False)) for k in NEEDS[1:]})


def _rule(where: str, layer: str, document: str, table: dict) -> Rule:
    rule_id = str(table.get("id", "")).strip()
    where = f"{where} rule {rule_id or '(no id)'}"
    if not rule_id:
        raise PolicyError(f"{where}: every rule has an id")
    if not str(table.get("says", "")).strip():
        raise PolicyError(f"{where}: says is what Tim tells a person about it, and it is required")
    if ";" in str(table["says"]):
        raise PolicyError(f"{where}: says reads as sentences, without semicolons")
    source = _source(where, document, table.get("source"))
    _unknown(where, table, _RULE_KEYS)
    caps = {k: _amount(where, k, table[k]) for k in CAPS if k in table}
    needs = _needs(where, table.get("needs"))
    if not caps and not needs.asks():
        raise PolicyError(
            f"{where}: a rule caps an amount or needs something; this one does neither"
        )
    return Rule(
        id=rule_id,
        layer=layer,
        says=str(table["says"]).strip(),
        source=source,
        who=_names(where, "who", table.get("who")),
        kinds=_names(where, "kinds", table.get("kinds")),
        over=_amount(where, "over", table.get("over", 0)),
        needs=needs,
        **caps,
    )


def _check_names(
    where: str,
    rule: Rule,
    *,
    roles: set[str] | None,
    people: set[str] | None,
    kinds: list[str] | None,
) -> Rule:
    if roles is not None and people is not None:
        known = roles | people
        for name in [w for w in rule.who if w != "*"] + (
            [rule.needs.approver] if rule.needs.approver else []
        ):
            if name not in known:
                raise PolicyError(
                    f"{where} rule {rule.id}: {name!r} is not a role or person the ministry named"
                )
    if kinds is not None:
        spelled = []
        for kind in rule.kinds:
            if kind == "*":
                spelled.append(kind)
                continue
            own = next((k for k in kinds if k.casefold() == kind.casefold()), None)
            if own is None:
                raise PolicyError(
                    f"{where} rule {rule.id}: {kind!r} is not one of the ministry's categories"
                )
            spelled.append(own)
        rule = Rule(**{**rule.__dict__, "kinds": tuple(spelled)})
    return rule


def parse_layer(
    data: dict,
    name: str,
    *,
    roles: set[str] | None = None,
    people: set[str] | None = None,
    kinds: list[str] | None = None,
) -> Layer:
    """One rules file. ``roles``/``people`` (from authority.toml) and
    ``kinds`` (the ministry's categories) are checked when given."""
    layer = data.get("layer")
    if layer not in LAYERS:
        raise PolicyError(f"{name}: layer = {layer!r}; use one of {', '.join(LAYERS)}")
    _unknown(name, data, _LAYER_KEYS)
    document = str(data.get("document", "")).strip()
    if not document:
        raise PolicyError(f"{name}: document names what these rules came from")
    person = str(data.get("person", "")).strip()
    if layer == "person":
        if not person:
            raise PolicyError(f'{name}: a person layer says person = "<who it covers>"')
        if people is not None and person not in people:
            raise PolicyError(f"{name}: {person!r} is not a person the ministry named")
    elif person:
        raise PolicyError(f"{name}: only a person layer names a person")
    tables = data.get("rule", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise PolicyError(f"{name}: rules are [[rule]] tables")
    rules: list[Rule] = []
    for table in tables:
        rule = _check_names(
            name, _rule(name, layer, document, table), roles=roles, people=people, kinds=kinds
        )
        if layer == "person":
            rule = Rule(**{**rule.__dict__, "who": (person,)})
        if any(r.id == rule.id for r in rules):
            raise PolicyError(f"{name}: two rules have the id {rule.id!r}")
        rules.append(rule)
    return Layer(layer, document, tuple(rules), person, name)


# ---- only stricter ----------------------------------------------------------------


def _overlap(a: tuple[str, ...], b: tuple[str, ...], *, fold: bool = False) -> bool:
    if "*" in a or "*" in b:
        return True
    norm = (lambda s: s.casefold()) if fold else (lambda s: s)
    return bool({norm(x) for x in a} & {norm(x) for x in b})


def looser(upper: Rule, lower: Rule) -> str:
    """Why ``lower`` reads looser than ``upper`` where the two overlap, or ""."""
    if not (_overlap(upper.who, lower.who) and _overlap(upper.kinds, lower.kinds, fold=True)):
        return ""
    for cap in CAPS:
        mine, theirs = getattr(lower, cap), getattr(upper, cap)
        if mine is not None and theirs is not None and mine > theirs:
            return f"{cap.replace('_', ' ')} {mine} is above {theirs}"
    shared = set(upper.needs.asks()) & set(lower.needs.asks())
    if shared and lower.over > upper.over:
        need = sorted(shared)[0].replace("approver:", "the OK of ")
        return f"it asks for {need} only over {lower.over}, not over {upper.over}"
    return ""


def check_stricter(layers: list[Layer]) -> None:
    """Refuse a rule in a lower layer that reads looser than one above it."""
    for i, lower_layer in enumerate(layers):
        for upper_layer in layers[:i]:
            if LAYERS.index(upper_layer.layer) >= LAYERS.index(lower_layer.layer):
                continue
            for lower in lower_layer.rules:
                for upper in upper_layer.rules:
                    why = looser(upper, lower)
                    if why:
                        raise PolicyError(
                            f"{lower_layer.file} rule {lower.id}: {why}, looser than "
                            f"{upper.id} ({upper.cite()}). A {lower.layer} may make a "
                            f"{upper.layer} rule only stricter."
                        )


def load_rulebook(
    tenant_dir: str | Path,
    *,
    roles: set[str] | None = None,
    people: set[str] | None = None,
    kinds: list[str] | None = None,
) -> Rulebook | None:
    """Every rules file under ``<tenant>/rules/``, highest layer first; None
    when the ministry has none (nothing changes for it)."""
    folder = Path(tenant_dir) / RULES_DIR
    files = sorted(folder.glob("*.toml")) if folder.is_dir() else []
    if not files:
        return None
    layers = []
    for path in files:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise PolicyError(f"{path.name} is not valid TOML: {exc}") from exc
        layers.append(parse_layer(data, path.name, roles=roles, people=people, kinds=kinds))
    layers.sort(key=lambda layer: LAYERS.index(layer.layer))
    check_stricter(layers)
    return Rulebook(tuple(layers))


# ---- after the spend: a month against the rules in force ----------------------------


@dataclass(frozen=True)
class Spent:
    """One line of a person's month, the way the rules see it."""

    key: str  # the line's id, never said to a person
    words: str  # how a person names it ("Dinner, Oct 3")
    kind: str  # the ministry's category
    cents: int
    receipt: bool  # a receipt stands behind it
    bank: bool = False  # it came from a bank statement or a school list


@dataclass(frozen=True)
class Finding:
    rule: Rule
    says: str  # the plain words a reviewer reads, with where the rule came from
    keys: tuple[str, ...]  # the lines it is about
    face_id: bool = False  # the rule asks for the person's own Face ID


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _said(rule: Rule, what: str, cents: int) -> str:
    return f"{what}, {_money(cents)}: {rule.says} ({rule.cite()})"


def _line_needs(rule: Rule, line: Spent) -> bool:
    """Whether this line falls under the rule's needs and is short of one we
    can see. Approver, before-buying and Face ID are always named from the
    amount on (the books cannot see a hallway OK); a receipt need is met by
    a receipt. A budget line is not checked until budgets exist."""
    if not rule.needs.asks() or line.cents <= rule.over * 100:
        return False
    n = rule.needs
    if n.approver or n.before or n.face_id:
        return True
    if n.receipt and not line.receipt:
        return True
    return n.receipt_not_bank and line.bank and not line.receipt


def check_month(
    rules: list[Rule],
    spent: list[Spent],
    *,
    person: str,
    roles: tuple[str, ...],
    year_before: dict[str, int] | None = None,
) -> list[Finding]:
    """What the rules in force say about this person's month, rule by rule
    in their layer order. ``year_before``: cents per category earlier in the
    ministry's year, for the yearly caps. Code, never a model."""
    year_before = year_before or {}
    found: list[Finding] = []
    for rule in rules:
        mine = [s for s in spent if rule.covers(person, roles, s.kind)]
        if not mine:
            continue
        if rule.item_up_to is not None:
            for s in mine:
                if s.cents > rule.item_up_to * 100:
                    found.append(Finding(rule, _said(rule, s.words, s.cents), (s.key,)))
        label = "Spending" if "*" in rule.kinds else ", ".join(rule.kinds)
        total = sum(s.cents for s in mine)
        if rule.month_up_to is not None and total > rule.month_up_to * 100:
            keys = tuple(s.key for s in mine)
            found.append(Finding(rule, _said(rule, f"{label} this month", total), keys))
        if rule.year_up_to is not None:
            before = sum(
                c
                for kind, c in year_before.items()
                if "*" in rule.kinds or kind.casefold() in {k.casefold() for k in rule.kinds}
            )
            if before + total > rule.year_up_to * 100:
                keys = tuple(s.key for s in mine)
                found.append(Finding(rule, _said(rule, f"{label} this year", before + total), keys))
        for s in mine:
            if _line_needs(rule, s):
                found.append(
                    Finding(rule, _said(rule, s.words, s.cents), (s.key,), rule.needs.face_id)
                )
    return found


__all__ = [
    "CAPS",
    "Finding",
    "Spent",
    "check_month",
    "LAYERS",
    "NEEDS",
    "RULES_DIR",
    "Layer",
    "Needs",
    "PolicyError",
    "Rule",
    "Rulebook",
    "Source",
    "check_stricter",
    "load_rulebook",
    "looser",
    "parse_layer",
]
