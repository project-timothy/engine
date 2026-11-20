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

A rules file may be a ministry's accountable plan (``plan = true``, chunk 3,
the owner's go of 2026-10-16): its rules also say how much cash a person may
hold (``cash_up_to``) and in how many days an advance is accounted for
(``account_within``) and what is left returned (``return_within``), which
the cash clock reads once the plan is adopted (``clock``). A plan goes in
force whole, on one yes, from the role it names (``adopted_by``), never
from anyone it covers.

A rules file may name the copy of its document it was read from
(``[copy]``: a ``file`` under ``rules/`` or an https ``url``, and the
``sha`` of that copy). ``policy watch`` reads the copy every morning and
on demand, and ``find_quote`` says whether each rule's words still read in
it (chunk 8, the owner's ask of 2026-10-15).

Nothing here reads a ledger, a card, or a tenant's config: callers pass the
ministry's roles, people and categories, and get rules back.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

LAYERS = ("handbook", "executive", "region", "tenant", "person")
"""Highest first. A layer may only make the ones above it stricter."""

CAPS = ("item_up_to", "month_up_to", "year_up_to")
NEEDS = ("approver", "before", "face_id", "receipt", "receipt_not_bank", "budgeted")
"""What a rule may ask for from its ``over`` amount on. ``approver`` names a
role (or person) whose OK it needs; the rest are yes/no, and only "yes" says
anything, so a lower layer cannot switch one off by writing "no"."""

PLAN = ("cash_up_to", "account_within", "return_within")
"""What an accountable plan adds: the cash a person may hold, and the days
an advance has to be accounted for and what is left returned."""

PAY = ("pay_year", "pay_day", "pay_from")
"""What a pay resolution adds (c34, the sender model): the yearly amount, the
day of the month it is paid, and the month it starts (``YYYY-MM``). It runs
through that December; the board sets it again each year."""

RULES_DIR = "rules"

_RULE_KEYS = frozenset(
    {"id", "who", "kinds", "over", "says", "needs", "source", "replaces", *CAPS, *PLAN, *PAY}
)
_SOURCE_KEYS = frozenset({"document", "section", "page", "quote"})
_LAYER_KEYS = frozenset({"layer", "document", "person", "rule", "copy", "plan", "adopted_by"})
_COPY_KEYS = frozenset({"file", "url", "sha"})


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
    cash_up_to: Decimal | None = None
    account_within: int | None = None
    return_within: int | None = None
    pay_year: Decimal | None = None
    pay_day: int | None = None
    pay_from: str | None = None
    replaces: str = ""
    """The rule in the same file this one takes the place of once it is in
    force (a draft from a new handbook copy, t-rule-drafting); until then
    the one it replaces keeps counting."""

    @property
    def sha(self) -> str:
        """The rule's fingerprint: every word and amount in it, so a yes
        given to one wording never covers another. A plan or pay setting a
        rule does not say is left out, and so is an empty ``replaces``, so
        every rule written before any of them keeps the fingerprint its yes
        was given to."""
        fields = {
            k: v
            for k, v in asdict(self).items()
            if (k not in PLAN + PAY or v is not None) and (k != "replaces" or v)
        }
        body = json.dumps(fields, sort_keys=True, default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def cite(self) -> str:
        """Where it came from, the way a person names a page."""
        parts = [self.source.document, self.source.section]
        if self.source.page is not None:
            parts.append(f"p. {self.source.page}")
        return ", ".join(p for p in parts if p)

    @property
    def spending(self) -> bool:
        """Whether it says anything about a purchase (a cap or a need); a
        plan's cash and days are about cash in hand instead."""
        return any(getattr(self, k) is not None for k in CAPS) or bool(self.needs.asks())

    @property
    def pays(self) -> bool:
        """Whether it is a pay resolution's rule (c34)."""
        return self.pay_year is not None

    def covers(self, person: str, roles: tuple[str, ...], kind: str) -> bool:
        what = "*" in self.kinds or kind.casefold() in {k.casefold() for k in self.kinds}
        return self.covers_person(person, roles) and what

    def covers_person(self, person: str, roles: tuple[str, ...]) -> bool:
        return "*" in self.who or person in self.who or bool(set(roles) & set(self.who))


@dataclass(frozen=True)
class Copy:
    """The copy of a document its rules were read from: a ``file`` under the
    ministry's ``rules/`` folder or an https ``url``, and the ``sha`` (sha256)
    of the copy the rules were drafted against. ``path`` is the file, found."""

    file: str = ""
    url: str = ""
    sha: str = ""
    path: str = ""


@dataclass(frozen=True)
class Layer:
    layer: str
    document: str
    rules: tuple[Rule, ...]
    person: str = ""
    file: str = ""
    copy: Copy | None = None
    plan: bool = False
    adopted_by: str = ""

    @property
    def covers(self) -> tuple[str, ...]:
        """Everyone a plan's rules cover, as the rules name them (roles,
        people, or "*"): the people who never adopt it."""
        return tuple(sorted({w for r in self.rules for w in r.who}))


@dataclass(frozen=True)
class Rulebook:
    layers: tuple[Layer, ...]

    @property
    def rules(self) -> list[Rule]:
        return [r for layer in self.layers for r in layer.rules]

    def in_force(self, approved: set[str]) -> list[Rule]:
        """The rules a person said yes to, word for word, highest layer
        first, less any whose place an approved draft has taken."""
        out = []
        for layer in self.layers:
            yes = [r for r in layer.rules if r.sha in approved]
            taken = {r.replaces for r in yes if r.replaces}
            out += [r for r in yes if r.id not in taken]
        return out

    @property
    def asks_first(self) -> bool:
        """Whether any rule asks for an OK (an approver, or before buying):
        only then is there anything to ask for ahead of a purchase."""
        return any(r.needs.approver or r.needs.before for r in self.rules)

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


def _days(where: str, key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 366:
        raise PolicyError(f"{where}: {key} is a number of days, 1 to 366, not {value!r}")
    return value


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


parse_source = _source
"""A source table read as a rule's is, for a pack's numbers (core/pack)."""


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


def _copy(where: str, table: object) -> Copy | None:
    if table is None:
        return None
    if not isinstance(table, dict):
        raise PolicyError(f"{where}: copy is a table")
    _unknown(f"{where} copy", table, _COPY_KEYS)
    file, url, sha = (str(table.get(k, "")).strip() for k in ("file", "url", "sha"))
    if bool(file) == bool(url):
        raise PolicyError(f"{where}: copy names a file or a url, one of them")
    if url:
        from ..engine import web  # the engine's one web rule; here, not at the top: a cycle

        try:
            web.checked(url)
        except web.SchemeRefused as exc:
            raise PolicyError(f"{where}: copy url is https ({exc})") from exc
    if file and (Path(file).is_absolute() or ".." in Path(file).parts):
        raise PolicyError(f"{where}: copy file is a path under rules/")
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise PolicyError(f"{where}: copy sha is the copy's sha256, 64 hex digits")
    return Copy(file=file, url=url, sha=sha)


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
    if "cash_up_to" in table:
        caps["cash_up_to"] = _amount(where, "cash_up_to", table["cash_up_to"])
    caps.update({k: _days(where, k, table[k]) for k in PLAN[1:] if k in table})
    needs = _needs(where, table.get("needs"))
    pay = _pay(where, table)
    if pay and (caps or needs.asks() or "over" in table):
        raise PolicyError(f"{where}: pay says nothing else; put any cap or need in its own rule")
    if pay:
        who = table.get("who")
        if not isinstance(who, list) or len(who) != 1 or who[0] == "*":
            raise PolicyError(f"{where}: pay names the one person it pays, in who")
        caps.update(pay)
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
        replaces=str(table.get("replaces", "")).strip(),
        **caps,
    )


MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _pay(where: str, table: dict) -> dict:
    """A pay rule's yearly amount, day and first month; {} for any other."""
    said = [k for k in PAY if k in table]
    if not said:
        return {}
    if len(said) != len(PAY):
        raise PolicyError(
            f"{where}: pay says a yearly amount, a day of the month, and the month it starts "
            "(pay_year, pay_day, pay_from)"
        )
    yearly = _amount(where, "pay_year", table["pay_year"])
    if yearly <= 0:
        raise PolicyError(f"{where}: pay_year is more than $0")
    day = table["pay_day"]
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 28:
        raise PolicyError(f"{where}: pay_day is a day of the month, 1 to 28, not {day!r}")
    start = table["pay_from"]
    if not isinstance(start, str) or not MONTH.match(start):
        raise PolicyError(f"{where}: pay_from is a month, like 2027-01, not {start!r}")
    return {"pay_year": yearly, "pay_day": day, "pay_from": start}


def _check_names(
    where: str,
    rule: Rule,
    *,
    roles: set[str] | None,
    people: set[str] | None,
    kinds: list[str] | None,
) -> Rule:
    if rule.pays and people is not None and rule.who[0] not in people:
        raise PolicyError(
            f"{where} rule {rule.id}: pay names a person the ministry named, not {rule.who[0]!r}"
        )
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
    ids = {r.id for r in rules}
    for rule in rules:
        if rule.replaces and (rule.replaces == rule.id or rule.replaces not in ids):
            raise PolicyError(
                f"{name} rule {rule.id}: replaces {rule.replaces!r}, which is not another "
                "rule in this file"
            )
    plan, adopted_by = _plan(name, data, rules, roles=roles)
    if not plan and any(getattr(r, k) is not None for r in rules for k in PLAN):
        raise PolicyError(f"{name}: cash and days on hand belong to a plan (plan = true)")
    if not plan and any(r.pays for r in rules):
        raise PolicyError(f"{name}: pay and its day belong to a plan (plan = true)")
    return Layer(
        layer,
        document,
        tuple(rules),
        person,
        name,
        _copy(name, data.get("copy")),
        plan,
        adopted_by,
    )


def _plan(name: str, data: dict, rules: list[Rule], *, roles: set[str] | None) -> tuple[bool, str]:
    """``plan = true`` and the role whose yes adopts it. Nobody a plan
    covers adopts it, so the role it names is one its rules do not."""
    plan = data.get("plan", False)
    if not isinstance(plan, bool):
        raise PolicyError(f"{name}: plan is true or false")
    adopted_by = str(data.get("adopted_by", "")).strip()
    if not plan:
        if adopted_by:
            raise PolicyError(f"{name}: only a plan (plan = true) names who adopts it")
        return False, ""
    if not adopted_by:
        raise PolicyError(f'{name}: a plan says who adopts it: adopted_by = "<a role>"')
    if roles is not None and adopted_by not in roles:
        raise PolicyError(f"{name}: adopted_by {adopted_by!r} is not one of the ministry's roles")
    if not rules:
        raise PolicyError(f"{name}: a plan has rules")
    covered = {w for r in rules for w in r.who}
    if "*" in covered or adopted_by in covered:
        raise PolicyError(
            f"{name}: the plan covers whoever adopts it ({adopted_by}); "
            "it names the people it covers, and someone else adopts it"
        )
    return True, adopted_by


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
    for cap in (*CAPS, *PLAN):
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
        layer = parse_layer(data, path.name, roles=roles, people=people, kinds=kinds)
        if layer.copy is not None and layer.copy.file:
            found = (folder / layer.copy.file).resolve()
            if not found.is_relative_to(folder.resolve()):
                raise PolicyError(f"{path.name}: copy file is a path under rules/")
            layer = replace(layer, copy=replace(layer.copy, path=str(found)))
        layers.append(layer)
    layers.sort(key=lambda layer: LAYERS.index(layer.layer))
    check_stricter(layers)
    return Rulebook(tuple(layers))


# ---- the words still read: a rule's quote in a copy of its document ---------------

_SAME = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00ad": None,
    }
)


def _letters(text: str) -> str:
    """Text the way a quote is matched: one Unicode form, straight quotes,
    and no hyphens, dashes, or spaces at all, since a PDF's text layer drops
    and adds spaces between words and breaks a word across a line with one."""
    text = unicodedata.normalize("NFKC", text).translate(_SAME).casefold()
    return re.sub(r"[\s-]+", "", text)


def find_quote(quote: str, pages: list[str]) -> int | None:
    """The page (from 1, in the copy's own order) where ``quote`` starts, or
    None when its words are not in the copy. A quote may run across a page
    break. Page numbers are the file's, which a printed book may number
    differently, so a rule's cited page is never held against it."""
    want = _letters(quote)
    if not want:
        return None
    starts, text = [], ""
    for page in pages:
        starts.append(len(text))
        text += _letters(page)
    at = text.find(want)
    if at < 0:
        return None
    return max(i for i, s in enumerate(starts) if s <= at) + 1


# ---- the cash clock: what an adopted plan says about cash in hand -----------------


@dataclass(frozen=True)
class Clock:
    """Days cash may sit unaccounted for (a nudge, the plan's due day, and
    the day what is left goes back), and the most cash a person may hold.
    The defaults are what Tim said before any plan was adopted."""

    due: int = 60
    back: int = 120
    cash_up_to: Decimal | None = None

    @property
    def nudge(self) -> int:
        """Two weeks before it is due (45 of 60), never before day one."""
        return max(self.due - 15, 1)


def clock(rules: list[Rule], *, person: str, roles: tuple[str, ...]) -> Clock:
    """The strictest numbers the rules in force set for this person, each
    left at its default where none does. Code, never a model."""
    mine = [r for r in rules if r.covers_person(person, roles)]
    due = [r.account_within for r in mine if r.account_within is not None]
    back = [r.return_within for r in mine if r.return_within is not None]
    held = [r.cash_up_to for r in mine if r.cash_up_to is not None]
    base = Clock()
    return Clock(
        due=min(due, default=base.due),
        back=min(back, default=base.back),
        cash_up_to=min(held, default=None),
    )


# ---- pay: what a board's pay resolution says for a month (c34) --------------------


@dataclass(frozen=True)
class Pay:
    """One person's pay in a month: the yearly amount, this month's part of
    it, the day it is paid, the month the resolution starts, and its rule."""

    person: str
    yearly_cents: int
    cents: int
    day: int
    start: str  # YYYY-MM
    rule: Rule


def money_words(amount: Decimal) -> str:
    """Dollars as a board reads them: "$1,500", or "$1,512.50" with cents
    (the accountable plan's words and a pay resolution's)."""
    return f"${amount:,.0f}" if amount == amount.to_integral() else f"${amount:,.2f}"


def monthly_cents(yearly_cents: int, month: int) -> int:
    """A month's part of a yearly amount: a twelfth, to the cent, with what
    does not split evenly paid in December so the year adds up exactly."""
    part = yearly_cents // 12
    return part + (yearly_cents - 12 * part if month == 12 else 0)


def pay_for(rules: list[Rule], person: str, month: str) -> Pay | None:
    """The pay in force for ``person`` in ``month`` (``YYYY-MM``): of the pay
    rules given (the ones adopted), the latest to start on or before that
    month in the same year. None when no resolution covers the month. Code,
    never a model."""
    year = month[:4]
    mine = [
        r
        for r in rules
        if r.pays
        and r.who == (person,)
        and r.pay_from
        and year == r.pay_from[:4]
        and r.pay_from <= month
    ]
    if not mine:
        return None
    rule = max(mine, key=lambda r: r.pay_from or "")
    yearly = int((rule.pay_year or Decimal(0)) * 100)
    return Pay(
        person=person,
        yearly_cents=yearly,
        cents=monthly_cents(yearly, int(month[5:7])),
        day=rule.pay_day or 1,
        start=rule.pay_from or month,
        rule=rule,
    )


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
    day: str = ""  # YYYY-MM-DD, when the books know it
    ok: tuple[tuple[str, str], ...] = ()  # (rule sha, who said yes) given before the spend


@dataclass(frozen=True)
class Ok:
    """A yes given before the spend (chunk 7, an approved ``policy.ask``
    card): who gave it, for what kind of spending, up to how much, from
    which day, and under which rules (their fingerprints)."""

    by: str
    kind: str
    cents: int
    since: str  # YYYY-MM-DD, the day it was asked for
    rules: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    rule: Rule
    says: str  # the plain words a reviewer reads, with where the rule came from
    keys: tuple[str, ...]  # the lines it is about
    face_id: bool = False  # the rule asks for the person's own Face ID
    met: bool = False  # a yes given before the spend met it; said so the reviewer sees it


def with_oks(spent: list[Spent], oks: list[Ok]) -> list[Spent]:
    """Each yes given before the spend covers ONE line: the earliest of its
    kind, on or after the day it was asked for, at or under its amount, not
    already covered under the same rules. A line whose day the books do not
    know is never covered, so nothing is assumed. Code, never a model."""
    out = list(spent)
    order = sorted(range(len(out)), key=lambda i: (out[i].day, i))
    for ok in sorted(oks, key=lambda o: (o.since, o.cents)):
        for i in order:
            s = out[i]
            if (
                not s.day
                or s.day < ok.since
                or s.cents > ok.cents
                or s.kind.casefold() != ok.kind.casefold()
                or any(sha in ok.rules for sha, _by in s.ok)
            ):
                continue
            out[i] = replace(s, ok=s.ok + tuple((sha, ok.by) for sha in ok.rules))
            break
    return out


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _said(rule: Rule, what: str, cents: int) -> str:
    return f"{what}, {_money(cents)}: {rule.says} ({rule.cite()})"


def _ok_by(rule: Rule, line: Spent) -> str:
    """Who said yes to this line under this rule before it was bought."""
    return next((by for sha, by in line.ok if sha == rule.sha), "")


def _line_needs(rule: Rule, line: Spent) -> bool:
    """Whether this line falls under the rule's needs and is short of one we
    can see. Approver and before-buying are met only by a yes asked for
    before the spend (``Spent.ok``; the books cannot see a hallway OK);
    Face ID is always named from the amount on; a receipt need is met by a
    receipt. A budget line is not checked until budgets exist."""
    if not rule.needs.asks() or line.cents <= rule.over * 100:
        return False
    n = rule.needs
    if (n.approver or n.before) and not _ok_by(rule, line):
        return True
    if n.face_id:
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
            elif _ok_by(rule, s) and s.cents > rule.over * 100:
                said = (
                    f"{s.words}, {_money(s.cents)}: OK'd by {_ok_by(rule, s)} before it was bought"
                )
                found.append(Finding(rule, f"{said} ({rule.cite()})", (s.key,), met=True))
    return found


__all__ = [
    "CAPS",
    "PLAN",
    "PAY",
    "MONTH",
    "Pay",
    "monthly_cents",
    "money_words",
    "pay_for",
    "Clock",
    "clock",
    "Copy",
    "find_quote",
    "Finding",
    "Spent",
    "check_month",
    "LAYERS",
    "parse_source",
    "NEEDS",
    "RULES_DIR",
    "Layer",
    "Needs",
    "Ok",
    "PolicyError",
    "Rule",
    "Rulebook",
    "Source",
    "check_stricter",
    "load_rulebook",
    "looser",
    "parse_layer",
    "with_oks",
]
