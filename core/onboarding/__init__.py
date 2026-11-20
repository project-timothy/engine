"""The onboarding conversation (docs/tenant-kit-design.md, section 6; #442).

An agent runs the conversation; the engine holds the questions
(``questions.toml``) and every rule about them. The flow:

1. ``next_question(answers)``: the next question this shape asks that has no
   answer yet, with its choices and its default for the shape.
2. ``record(answers, id, value)``: the answer, checked and normalized.
3. ``plan(answers, slug)``: every answer resolved into what ``engine init``
   and the kit need; refuses while a required answer is missing.
4. ``apply(plan, root=...)``: renders the tenant, writes the answers into
   ``tenant.toml``, ``authority.toml`` and ``kit/voice.toml``, checks that all
   of it loads, and runs doctor.

``engine onboard`` exposes the same flow as JSON, so any agent can drive it.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from ..authority import parse_policy
from ..engine.config import load_tenant
from ..engine.doctor import DoctorReport, run_doctor
from ..engine.init import InitResult, init_tenant, render
from ..engine.kit import SHAPES, family_of, load_kit
from ..pack import PACK_FILE, Pack, load_pack
from ..policy import RULES_DIR
from . import accountable
from .zones import ZoneAnswerError, resolve_zone

QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.toml"
SAID: dict[str, str] = tomllib.loads(QUESTIONS_FILE.read_text(encoding="utf-8")).get("said", {})
"""What setup says when it asks an answer again in its own words."""

SELF_ROLE = {"commercial-solo": "owner", "nonprofit-solo": "missionary"}
"""The role the person answering holds in a solo shape (they run it)."""

REVIEWER_ROLE = {"commercial-solo": "accountant", "nonprofit-solo": "reviewer"}
"""The role a solo tenant's outside reviewer holds: view and the monthly review."""


class OnboardingError(ValueError):
    """An answer the conversation cannot take, or an apply it cannot do yet."""


@dataclass(frozen=True)
class Question:
    id: str
    ask: str
    kind: str
    help: str = ""
    choices: tuple[dict, ...] = ()
    sizes: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    optional: bool = False
    default: str | None = None
    defaults: dict = field(default_factory=dict)
    needs: str = ""
    """Another question whose answer this one follows: asked only when that
    one was answered (a month approver only when someone was named)."""

    def applies_to(self, shape: str | None) -> bool:
        if shape is None:
            return not self.sizes and not self.families
        family, size = shape.split("-", 1)
        return (not self.sizes or size in self.sizes) and (
            not self.families or family in self.families
        )

    def default_for(self, shape: str | None) -> str | None:
        if shape is not None and shape in self.defaults:
            return str(self.defaults[shape])
        return self.default


@cache
def load_questions() -> tuple[Question, ...]:
    with QUESTIONS_FILE.open("rb") as handle:
        data = tomllib.load(handle)
    return tuple(
        Question(
            id=q["id"],
            ask=q["ask"],
            kind=q["kind"],
            help=q.get("help", ""),
            choices=tuple(q.get("choices", [])),
            sizes=tuple(q.get("sizes", [])),
            families=tuple(q.get("families", [])),
            optional=bool(q.get("optional", False)),
            default=q.get("default"),
            defaults=dict(q.get("defaults", {})),
            needs=str(q.get("needs", "")),
        )
        for q in data["question"]
    )


def _question(qid: str) -> Question:
    for q in load_questions():
        if q.id == qid:
            return q
    raise OnboardingError(f"no question {qid!r}")


@cache
def _shape_policy(shape: str):
    """The default authority policy ``shape`` renders with."""
    files = render("preview", "A", shape=shape, data_root_rel="preview-data")
    return parse_policy(tomllib.loads(files["authority.toml"]))


@cache
def people_roles(shape: str) -> tuple[str, ...]:
    """The roles a person can hold in ``shape``: the shape's defaults, minus
    the roles only its agents hold."""
    policy = _shape_policy(shape)
    agent_roles = {r for a in policy.agents.values() for r in a.roles}
    return tuple(r for r in policy.roles if r not in agent_roles)


def _shape(answers: dict) -> str | None:
    shape = answers.get("who")
    return shape if shape in SHAPES else None


PACK_SAYS = frozenset(
    {"second_person_above", "two_approvals_above", "yes_by_word_up_to", "mileage_rate"}
)
"""The questions a pack answers itself: its numbers, each cited (core/pack).
A sending agency sets its own mileage rate in its handbook."""


@cache
def _load_pack(directory: str) -> Pack:
    return load_pack(directory)


def _pack(answers: dict) -> Pack | None:
    """The agency pack this ministry is set up from, if the host named one
    (``engine mcp --onboarding --pack``); a person never names it."""
    where = answers.get("pack")
    return _load_pack(str(where)) if where else None


def _roles(answers: dict, shape: str) -> tuple[str, ...]:
    pack = _pack(answers)
    return pack.roles if pack is not None else people_roles(shape)


def _policy(answers: dict, shape: str):
    pack = _pack(answers)
    return parse_policy(pack.authority) if pack is not None else _shape_policy(shape)


def next_question(answers: dict) -> dict | None:
    """The next unanswered question for this shape, ready for an agent to ask:
    id, ask, help, kind, choices, default, optional. None when done."""
    shape = _shape(answers)
    pack = _pack(answers)
    for q in load_questions():
        if q.id in answers or not q.applies_to(shape):
            continue
        if q.needs and not answers.get(q.needs):
            continue
        if pack is not None and q.id in PACK_SAYS:
            continue
        if shape is None and q.id != "who":
            continue
        out: dict = {"id": q.id, "ask": q.ask, "kind": q.kind, "optional": q.optional}
        if q.help:
            out["help"] = q.help
        if q.kind == "choice":
            out["choices"] = [c["label"] for c in q.choices]
        if q.kind in ("role", "people") and shape:
            out["choices"] = list(_roles(answers, shape))
        default = q.default_for(shape)
        if default is not None:
            out["default"] = default
        return out
    return None


# ---- recording an answer ------------------------------------------------------------


def _amount(qid: str, value: object) -> str:
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise OnboardingError(f"{qid}: {value!r} is not an amount") from exc
    if amount < 0:
        raise OnboardingError(f"{qid}: an amount cannot be negative")
    return str(int(amount)) if amount == amount.to_integral_value() else str(amount.normalize())


_IRS_SAID = ("", "irs", "irs rate", "the irs rate", "irs mileage rate", "the irs mileage rate")


def _mileage(qid: str, value: object, today: date) -> str:
    """The ministry's rate per mile in dollars ("0.65"), or "" for the IRS
    rate for the year (the default, the owner 2026-11-16). A plain number of 1
    or more is cents ("65", "65 cents", "70.5"); with a dollar sign, or under
    1, it is dollars ("$0.65", "0.7"). Above today's IRS rate is refused."""
    text = str(value).strip().casefold().rstrip(".")
    if text in _IRS_SAID:
        return ""
    dollars_said = "$" in text
    number = re.sub(r"\s*(cents?|¢|a mile|per mile)\s*", " ", text.replace("$", "")).strip()
    try:
        said = Decimal(number)
    except InvalidOperation as exc:
        raise OnboardingError(
            f"{qid}: {value!r} is not a rate; say IRS rate, or cents a mile, like 65"
        ) from exc
    if said <= 0:
        raise OnboardingError(f"{qid}: a rate is more than 0 cents a mile")
    rate = said if dollars_said or said < 1 else said / 100
    from ..institution import InstitutionError, irs_mileage_rate

    try:
        irs = irs_mileage_rate(today)
    except InstitutionError as exc:
        raise OnboardingError(f"{qid}: {exc}") from exc
    if rate > irs:
        raise OnboardingError(
            f"{qid}: {(rate * 100).normalize():f} cents a mile is above the IRS rate of "
            f"{(irs * 100).normalize():f} cents today; "
            "anything above it counts as pay. Say IRS rate, or a rate at or below it."
        )
    shown = f"{rate.normalize():f}"
    return shown if len(shown.partition(".")[2]) >= 2 else f"{rate:.2f}"


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _month(qid: str, value: object) -> str:
    """A month as its number or its English name ("July", "jul", "sept")."""
    text = str(value).strip().casefold()
    if text.isdigit():
        if not 1 <= int(text) <= 12:
            raise OnboardingError(f"{qid}: a month is 1 to 12")
        return str(int(text))
    if len(text) >= 3:
        for number, prefix in enumerate(MONTHS, start=1):
            if text.startswith(prefix) or (prefix == "sep" and text.startswith("sept")):
                return str(number)
    raise OnboardingError(f"{qid}: {value!r} is not a month; a name or a number 1 to 12")


def _role(qid: str, said: str, roles: tuple[str, ...]) -> str:
    """The one role ``said`` names, found among the words around it
    ("volunteer treasurer" is treasurer, "missionary in Nepal" is missionary).
    No role, or two, refuses: the person is asked again."""
    text = said.strip()
    if text in roles:
        return text
    words = re.findall(r"[a-z]+", text.casefold())
    found = [
        role
        for role in roles
        if (parts := re.findall(r"[a-z]+", role))
        and any(words[i : i + len(parts)] == parts for i in range(len(words)))
    ]
    if len(found) == 1:
        return found[0]
    raise OnboardingError(f"{qid}: {said!r} is not one of {', '.join(roles)}")


def _items(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in re.split(r"[;\n,]", str(value)) if part.strip()]


_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _email_in(qid: str, text: str) -> tuple[str, str]:
    """(``text`` without its email address, the address). Two addresses in
    one answer are asked again: one address per person."""
    found = _EMAIL.findall(text)
    if len(found) > 1:
        raise OnboardingError(f"{qid}: one email address per person, please")
    return _EMAIL.sub(" ", text), (found[0].casefold() if found else "")


def name_and_email(qid: str, said: str) -> tuple[str, str]:
    """A person's name apart from the words and address around it: "Linda
    Park, board treasurer, linda@livingwatermz.org" is Linda Park at
    linda@livingwatermz.org. The name is the first part before a comma, a
    ``<``, a parenthesis or a bracket (walkthrough 2 made "Linda Park, board
    treasurer, linda@..." one long person id)."""
    rest, email = _email_in(qid, str(said).strip())
    parts = (p.strip(" \t<>()[],;-") for p in re.split(r"[,<(\[]", rest))
    name = " ".join(next((p for p in parts if p), "").split())
    if not name and email:
        raise OnboardingError(f"{qid}: whose address is {email}? Their name, please")
    return name, email


_PLACE = re.compile(r"\b(?:based in|living in|lives in|in|from)\s+(?:the\s+)?([^,;]+?)\s*$", re.I)


def _you(answers: dict) -> tuple[str, str]:
    """The person setting up, and their address when they gave one (the
    invitation at apply mails them their welcome link too). An answer saved
    before the address was asked is a name alone."""
    you = answers["your_name"]
    if isinstance(you, dict):
        return str(you.get("name", "")), str(you.get("email", ""))
    return str(you), ""


def _place_zone(words: str) -> str:
    """The zone the place in a person's line names ("missionary in Nepal"),
    or "" when it names none or more than one: that person keeps the
    ministry's zone, which the plan shows beside them."""
    found = _PLACE.search(words.strip(" ,;"))
    if not found:
        return ""
    try:
        return resolve_zone(found.group(1))
    except ZoneAnswerError:
        return ""


def _people(qid: str, value: object, roles: tuple[str, ...]) -> list[dict]:
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        entries = [
            (str(v.get("name", "")), str(v.get("role", "")), str(v.get("email", ""))) for v in value
        ]
    else:
        lines = [
            p
            for p in re.split(
                r"[;\n]", str(value) if not isinstance(value, list) else "\n".join(value)
            )
            if p.strip()
        ]
        entries = []
        for line in lines:
            if "=" not in line:
                raise OnboardingError(f"{qid}: {line.strip()!r} is not 'name = what they do'")
            rest, email = _email_in(qid, line)
            name, role = rest.split("=", 1)
            entries.append((name.strip(), role.strip(), email))
    people = []
    for name, role, email in entries:
        if not name:
            raise OnboardingError(f"{qid}: a person needs a name")
        person = {"name": name, "role": _role(qid, role, roles)}
        if email:
            person["email"] = email
        if zone := _place_zone(role):
            person["zone"] = zone
        people.append(person)
    return people


def _folded(name: str) -> str:
    """A name the way setup compares names: its words, whatever their case."""
    return " ".join(name.split()).casefold()


def _yourself(qid: str, entry: dict, answers: dict, shape: str) -> bool:
    """Whether a people line is the person setting up (t-setup-self-twice):
    their name, and no address apart from theirs to say it's someone else
    with their name. Such a line with another role is asked again."""
    if not answers.get("your_name"):
        return False
    you, your_email = _you(answers)
    if _folded(entry["name"]) != _folded(you):
        return False
    email = entry.get("email", "")
    if email and your_email and email.casefold() != your_email.casefold():
        return False  # two people who share a name, said plainly
    yours = SELF_ROLE.get(shape) or str(answers.get("your_role", ""))
    if yours and entry["role"] != yours:
        said = SAID["yourself_twice"].format(name=entry["name"], yours=yours)
        raise OnboardingError(f"{qid}: {said}")
    return True


def _named(answers: dict, shape: str) -> dict[str, tuple[str, str]]:
    """Everyone the conversation has named so far, by their name folded:
    (the name as given, their role)."""
    named: dict[str, tuple[str, str]] = {}
    if answers.get("your_name"):
        you, _email = _you(answers)
        named[you.casefold()] = (you, SELF_ROLE.get(shape) or str(answers.get("your_role", "")))
    for entry in answers.get("people", []) or []:
        named.setdefault(entry["name"].casefold(), (entry["name"], entry["role"]))
    return named


def _looks_after(qid: str, value: object, answers: dict, shape: str) -> list[dict]:
    """``Ann Lee = Sam Ortiz, Mei Tan``, one per line: who looks after whom.
    Every name must be one already given, and nobody who files their own
    reports looks after anyone else's (they could file and see a
    neighbor's)."""
    named = _named(answers, shape)

    def known(name: str) -> tuple[str, str]:
        found = named.get(" ".join(name.split()).casefold())
        if found is None:
            raise OnboardingError(f"{qid}: who is {name.strip()!r}? Add them to the people first")
        return found

    out = []
    for line in (p for p in re.split(r"[;\n]", str(value)) if p.strip()):
        if "=" not in line:
            raise OnboardingError(
                f"{qid}: {line.strip()!r} is not 'name = the people they look after'"
            )
        left, right = line.split("=", 1)
        name, role = known(left)
        if role and _files_expenses(_policy(answers, shape), role):
            raise OnboardingError(
                f"{qid}: {name} files their own reports, so they can't look after anyone else's"
            )
        charges = [known(c)[0] for c in right.split(",") if c.strip()]
        if not charges:
            raise OnboardingError(f"{qid}: whom does {name} look after?")
        out.append({"name": name, "looks_after": charges})
    return out


def _fits_pack(answers: dict, qid: str, value: str) -> None:
    pack = _pack(answers)
    if qid == "who" and pack is not None and value != pack.shape:
        raise OnboardingError(
            f"who: this ministry is set up from {pack.name}'s pack, a ministry organization"
        )


_PARSERS = {
    "month": lambda qid, said, _today: _month(qid, said),
    "amount": lambda qid, said, _today: _amount(qid, said),
    "mileage": _mileage,
}
"""The kinds whose answer is one value read from the words."""


def record(answers: dict, qid: str, value: object, *, today: date | None = None) -> dict:
    """``answers`` with ``qid`` answered; refuses an answer the question
    cannot take. The first answer must be ``who``."""
    q = _question(qid)
    shape = _shape(answers)
    if qid != "who" and shape is None:
        raise OnboardingError("answer who this is for first")
    text = value.strip() if isinstance(value, str) else value
    if not text and not q.optional and q.kind in ("text", "person"):
        raise OnboardingError(f"{qid}: needs an answer")
    if q.kind == "choice":
        wanted = str(text).casefold()
        for choice in q.choices:
            said_as = (choice["label"], str(choice["value"]), *choice.get("was", ()))
            if wanted in (s.casefold() for s in said_as):  # "was": a label it had before
                _fits_pack(answers, qid, str(choice["value"]))
                return {**answers, qid: choice["value"]}
        raise OnboardingError(
            f"{qid}: {value!r} is not one of {', '.join(c['label'] for c in q.choices)}"
        )
    if q.kind == "text":
        return {**answers, qid: str(text or "")}
    if q.kind == "person":
        if not text:
            return {**answers, qid: ""}
        name, email = name_and_email(qid, str(text))
        return {**answers, qid: {"name": name, "email": email} if email else name}
    if (parse := _PARSERS.get(q.kind)) is not None:
        return {**answers, qid: parse(qid, text or "", today or date.today())}
    if q.kind == "timezone":
        try:
            return {**answers, qid: resolve_zone(str(text or q.default_for(shape) or ""))}
        except ZoneAnswerError as exc:
            raise OnboardingError(f"{qid}: {exc}") from exc
    if q.kind == "list":
        return {**answers, qid: _items(text) if text else []}
    if shape is None:  # every question below this line depends on the shape
        raise OnboardingError(f"{qid}: answer who the books are for first")
    if q.kind == "role":
        return {**answers, qid: _role(qid, str(text), _roles(answers, shape))}
    if q.kind == "people":
        people = _people(qid, text, _roles(answers, shape)) if text else []
        for entry in people:
            _yourself(qid, entry, answers, shape)  # asks again when it can't tell
        return {**answers, qid: people}
    if q.kind == "looks_after":
        return {**answers, qid: _looks_after(qid, text, answers, shape) if text else []}
    raise OnboardingError(f"{qid}: unknown kind {q.kind!r}")


def missing(answers: dict) -> list[str]:
    """The required questions for this shape that have no answer yet."""
    shape = _shape(answers)
    if shape is None:
        return ["who"]
    return [
        q.id
        for q in load_questions()
        if q.applies_to(shape)
        and q.id not in answers
        and not q.optional
        and q.default_for(shape) is None
    ]


# ---- plan and apply ----------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    slug: str
    shape: str
    archetype: str
    legal_name: str
    timezone: str
    fiscal_start: int
    entity: str
    people: tuple[tuple[str, str, str], ...]  # (id, name, role)
    self_approval_limit: str | None
    two_approvals_above: str | None
    yes_by_word_up_to: str | None
    banned: tuple[str, ...]
    spelling: str
    emails: dict[str, str] = field(default_factory=dict)  # person id -> address, if given
    zones: dict[str, str] = field(default_factory=dict)  # person id -> their IANA zone
    looks_after: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )  # id -> their charges' ids
    cash_on_hand: str | None = None  # the accountable plan's ceiling, where the shape has one
    mileage_rate: str | None = None  # the ministry's own rate per mile; None = the IRS rate
    pack: str = ""  # the agency pack it is set up from (core/pack), if any
    reviewer_approves: bool = False
    """A solo ministry's reviewer also approves each month; the missionary
    can still approve her own, and they are told (board c48, 2026-11-17)."""


def _person_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "person"
    pid, n = base, 2
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    taken.add(pid)
    return pid


def plan(answers: dict, slug: str) -> Plan:
    """Every answer resolved, defaults applied. Refuses, naming each one,
    while a required answer is missing."""
    owed = missing(answers)
    if owed:
        raise OnboardingError(f"still to answer: {', '.join(owed)}")
    shape = str(answers["who"])
    pack = str(answers.get("pack") or "")

    def get(qid: str) -> object:
        if qid in answers:
            return answers[qid]
        return _question(qid).default_for(shape)

    taken: set[str] = set()
    people: list[tuple[str, str, str]] = []
    emails: dict[str, str] = {}
    you, your_email = _you(answers)
    your_role = SELF_ROLE.get(shape) or str(answers["your_role"])
    people.append((_person_id(you, taken), you, your_role))
    if your_email:
        emails[people[-1][0]] = your_email
    reviewer = answers.get("reviewer", "") or ""
    if isinstance(reviewer, dict):
        r_name, r_email = str(reviewer.get("name", "")), str(reviewer.get("email", ""))
    else:  # a name alone, or an answer saved before names and addresses came apart
        r_name, r_email = name_and_email("reviewer", str(reviewer)) if reviewer else ("", "")
    if r_name and shape in REVIEWER_ROLE:
        pid = _person_id(r_name, taken)
        people.append((pid, r_name, REVIEWER_ROLE[shape]))
        if r_email:
            emails[pid] = r_email
    ministry_zone = str(get("timezone"))
    zones = {pid: ministry_zone for pid, _n, _r in people}
    for entry in answers.get("people", []) or []:
        if _yourself("people", entry, answers, shape):  # the person setting up, once
            you_id = people[0][0]
            if entry.get("email") and you_id not in emails:
                emails[you_id] = entry["email"]
            if entry.get("zone"):
                zones[you_id] = entry["zone"]
            continue
        pid = _person_id(entry["name"], taken)
        people.append((pid, entry["name"], entry["role"]))
        if entry.get("email"):
            emails[pid] = entry["email"]
        zones[pid] = entry.get("zone") or ministry_zone
    ids: dict[str, str] = {}
    for pid, name, _role in people:
        ids.setdefault(name.casefold(), pid)
    looks_after: dict[str, tuple[str, ...]] = {}
    for entry in answers.get("looks_after", []) or []:
        # Asked again if the people changed since: every name must still be here.
        names = [entry["name"], *entry["looks_after"]]
        gone = [n for n in names if n.casefold() not in ids]
        if gone:
            raise OnboardingError(f"looks_after: who is {gone[0]!r}? Add them to the people first")
        pid = ids[entry["name"].casefold()]
        looks_after[pid] = looks_after.get(pid, ()) + tuple(
            ids[n.casefold()] for n in entry["looks_after"]
        )
    family = family_of(shape)
    entity = str(get("entity") if family == "commercial" else get("entity_nonprofit")) or ""
    size = shape.split("-", 1)[1]
    return Plan(
        slug=slug,
        shape=shape,
        archetype=str(get("work")) if family == "commercial" else "C",
        legal_name=str(answers["legal_name"]),
        timezone=ministry_zone,
        fiscal_start=int(str(get("fiscal_start"))),
        entity=entity,
        people=tuple(people),
        self_approval_limit=str(get("second_person_above"))
        if shape == "commercial-small"
        else None,
        two_approvals_above=str(get("two_approvals_above"))
        if size != "solo" and not pack
        else None,
        yes_by_word_up_to=None if pack else str(get("yes_by_word_up_to")),
        banned=tuple(_items(answers.get("banned_words", []) or [])),
        spelling=str(get("spelling")),
        emails=emails,
        zones=zones,
        looks_after=looks_after,
        cash_on_hand=str(get("cash_on_hand")) if shape in accountable.ADOPTED_BY else None,
        mileage_rate=str(answers.get("mileage_rate") or "") or None,
        pack=pack,
        reviewer_approves=shape == "nonprofit-solo"
        and bool(r_name)
        and get("reviewer_approves") == "approve",
    )


@dataclass
class OnboardResult:
    init: InitResult
    doctor: DoctorReport

    @property
    def tenant_dir(self) -> Path:
        return self.init.tenant_dir


def _set_line(path: Path, key: str, value: str) -> None:
    """Replace the one ``key = ...`` line the template renders."""
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(rf"(?m)^{re.escape(key)} = .*$", f"{key} = {value}", text)
    if count != 1:
        raise OnboardingError(f"{path.name}: expected one {key} line, found {count}")
    path.write_text(new, encoding="utf-8")


MONTH_GRANT = "approve:expense.report#month"


def _month_approver(authority: Path) -> None:
    """The reviewer approves each month and nothing else; the missionary
    keeps her own yes, and each time she uses it the card says so, for the
    reviewer to see (board c48, the owner's decision of 2026-11-17)."""
    text = authority.read_text(encoding="utf-8")
    old = '[roles.reviewer]\ngrants = ["view:*", "review:*"]'
    if text.count(old) != 1:
        raise OnboardingError("authority.toml: expected the reviewer's grants line once")
    new = f'[roles.reviewer]\ngrants = ["view:*", "review:*", "{MONTH_GRANT}"]'
    authority.write_text(text.replace(old, new), encoding="utf-8")
    _set_line(authority, "report_self_approvals", '"always"')


TEMPLATE_PEOPLE = ("Pat Owner", "Sam Staffer", "Vic Vendor")
"""The placeholder people ``engine init`` renders for an owner to replace."""


def _files_expenses(policy, role: str) -> bool:
    return any(g.action in ("submit", "*") for g in policy.roles[role].grants)


def persons_listed(text: str, filers: list[tuple[str, str]]) -> str:
    """tenant.toml's text with its persons list made ``filers`` ((name,
    role) each): an owner books as owner, everyone else as a reimbursed
    person."""
    rows = "".join(
        f"    {{name = {json.dumps(name)}, role = "
        f"{json.dumps('owner' if role == 'owner' else 'employee')}}},\n"
        for name, role in filers
    )
    text, count = re.subn(r"(?ms)^persons = \[\n.*?^\]$", f"persons = [\n{rows}]", text)
    if count != 1:
        raise OnboardingError("tenant.toml: expected one persons list")
    return text


def filer_folders(slug: str, directory: Path, names: list[str]) -> Path | None:
    """A drop folder for each person who files reports; the drop tree, or
    None where the tenant keeps none."""
    cfg = load_tenant(slug, tenants_root=directory.parent)
    drop = Path(cfg.expenses.drop_dir)
    if not (cfg.expenses.drop_dir and drop.is_dir()):
        return None
    for name in names:
        (drop / name).mkdir(exist_ok=True)
    return drop


def person_table(
    pid: str, name: str, role: str, scopes: list[str], email: str = "", zone: str = ""
) -> str:
    """One person's table in authority.toml, as setup writes it. The name is
    the person's own words, so a card that names them by name matches them
    (#603) and "nobody approves their own" never has to fall back."""
    return (
        f"\n[people.{pid}]\nname = {json.dumps(name)}\n"
        f"roles = [{json.dumps(role)}]\n"
        f"scopes = {json.dumps(scopes)}\n"
        + (f"email = {json.dumps(email)}\n" if email else "")
        + (f"zone = {json.dumps(zone)}\n" if zone else "")
    )


def _people_in_books(directory: Path, p: Plan, policy) -> None:
    """Put the answered people where the template has placeholders: who files
    expenses (anyone whose role may submit; an owner books as owner, everyone
    else as a reimbursed person), whose phone photos the inbox assumes (the
    first of them, the person answering), and the owners the close treats as
    owners (none in a nonprofit). Walkthrough 1 found Pat Owner, Sam Staffer
    and Vic Vendor filing a missionary's expenses."""
    filers = [(name, role) for _pid, name, role in p.people if _files_expenses(policy, role)]
    tenant = directory / "tenant.toml"
    tenant.write_text(persons_listed(tenant.read_text(encoding="utf-8"), filers), encoding="utf-8")
    _set_line(tenant, "inbox_person", json.dumps(filers[0][0] if filers else ""))
    owners = [name for _pid, name, role in p.people if role == "owner"]
    _set_line(tenant, "owner_names", json.dumps(owners))
    drop = filer_folders(p.slug, directory, [name for name, _role in filers])
    if drop is not None:
        for name in TEMPLATE_PEOPLE:
            folder = drop / name
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()


def _adopter(p: Plan, policy) -> str | None:
    """The role here that adopts the plan: the shape's own (a solo worker's
    reviewer, a church's board) when someone holds it, else the first role
    someone holds that reviews or confirms and files no expenses (renamed,
    say, to a director). None when nobody here can adopt it."""
    held = list(dict.fromkeys(role for _pid, _name, role in p.people))
    named = accountable.ADOPTED_BY[p.shape]
    if named in held:
        return named
    for role in held:
        grants = {g.action for g in policy.roles[role].grants} if role in policy.roles else set()
        if grants & {"review", "confirm"} and not _files_expenses(policy, role):
            return role
    return None


def _accountable_plan(directory: Path, p: Plan, policy) -> None:
    """The plan Tim writes for the board to adopt (chunk 3): it covers
    everyone who files expenses, and only when someone else is here to
    adopt it (a solo worker's reviewer, a church's board)."""
    if p.shape not in accountable.ADOPTED_BY or p.cash_on_hand is None:
        return
    adopter = _adopter(p, policy)
    if adopter is None:
        return
    # the ministry's own roles, not the shape's defaults (#583: a plan naming
    # roles nobody here holds never loads)
    agents = {r for a in policy.agents.values() for r in a.roles}
    covers = [
        r for r in policy.roles if r not in agents and r != adopter and _files_expenses(policy, r)
    ]
    written = accountable.write(
        p.legal_name, cash=Decimal(p.cash_on_hand), covers=covers, adopted_by=adopter
    )
    accountable.place(directory, written)


def form_990_due(fiscal_start: int, today: date) -> date:
    """The next Form 990 due date on or after ``today``: the 15th day of the
    fifth month after the fiscal year ends."""
    month = (fiscal_start - 1 + 4) % 12 + 1  # year ends the month before it starts; +5
    due = date(today.year, month, 15)
    return due if due >= today else date(today.year + 1, month, 15)


def _form_990(path: Path, fiscal_start: int, today: date) -> None:
    """A charity that files a Form 990 starts with the filing on its
    calendar (the alpha dress rehearsal, 2026-10-15: asked what was due, Tim
    had nothing, though setup had heard the answer)."""
    entry = (
        "\n[[obligation]]\n"
        'id = "form-990"\n'
        'title = "File the ministry\'s Form 990 with the IRS"\n'
        'kind = "tax"\n'
        f"due = {form_990_due(fiscal_start, today).isoformat()}\n"
        'every = "1y"\n'
        'notes = "Due the 15th day of the fifth month after the year ends. A Form 8868 '
        'gives six more months. A small charity may file the 990-N or 990-EZ instead."\n'
    )
    path.write_text(path.read_text(encoding="utf-8") + entry, encoding="utf-8")


def _lay_down(directory: Path, pack: Pack) -> None:
    """A pack's files as the new ministry's own: its authority file (setup
    adds the people), its rules and the copies they cite, the pack itself
    (so its numbers' words stay beside it), and its categories."""
    (directory / "authority.toml").write_text(
        f"# Set up from {pack.name}'s pack (core/pack). Every number here is cited, with\n"
        f"# the words it came from, in {PACK_FILE} beside this file.\n" + pack.authority_text,
        encoding="utf-8",
    )
    rules = pack.directory / RULES_DIR
    if rules.is_dir():
        shutil.copytree(rules, directory / RULES_DIR, dirs_exist_ok=True)
    shutil.copyfile(pack.directory / PACK_FILE, directory / PACK_FILE)
    config = directory / "tenant.toml"
    text = config.read_text(encoding="utf-8")
    marker = "[expenses.category_accounts]\n"
    listed = tomllib.loads(text)["expenses"]["category_accounts"]
    added = "".join(
        f"{json.dumps(c)} = {json.dumps(c)}\n" for c in pack.categories if c not in listed
    )
    config.write_text(text.replace(marker, marker + added, 1), encoding="utf-8")


def apply(
    p: Plan,
    *,
    root: str | Path | None = None,
    data_root: str | Path | None = None,
    ledger_dir: str | Path | None = None,
    run_audit: bool = True,
    today: date | None = None,
) -> OnboardResult:
    """Render the tenant and write the answers into it, then prove it loads
    and run doctor. Refuses before writing anything if the tenant exists."""
    result = init_tenant(
        p.slug,
        archetype=p.archetype,
        shape=p.shape,
        root=root,
        data_root=data_root,
        legal_name=p.legal_name,
        timezone=p.timezone,
        fiscal_year_start=p.fiscal_start,
        ledger_dir=ledger_dir,
        run_audit=run_audit,
    )
    directory = result.tenant_dir
    if p.pack:
        _lay_down(directory, _load_pack(p.pack))
    if p.entity:
        _set_line(directory / "tenant.toml", "entity", json.dumps(p.entity))
    if p.mileage_rate is not None:
        config = directory / "tenant.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + f'\n[institution]\nmileage_rate = "{p.mileage_rate}"\n',
            encoding="utf-8",
        )
    if p.entity == "public-charity":
        _form_990(directory / "obligations.toml", p.fiscal_start, today or date.today())
    authority = directory / "authority.toml"
    if p.self_approval_limit is not None:
        _set_line(authority, "self_approval_limit", p.self_approval_limit)
        _set_line(authority, "distinct_payer_above", p.self_approval_limit)
    if p.two_approvals_above is not None:
        _set_line(authority, "second_approver_above", p.two_approvals_above)
    if p.yes_by_word_up_to is not None:
        _set_line(authority, "yes_by_word_up_to", p.yes_by_word_up_to)
    if p.reviewer_approves:
        _month_approver(authority)
    # Each person's own id is their unit, so an @own grant covers their own
    # submissions; whoever looks after others (a regional director) holds
    # their units too, and the agent widens a scope later when the tenant
    # says so. An address the person gave rides beside them, for the sign-in
    # invitation, and so does their own zone, so the invitation (and Tim,
    # later) tells them times in it.
    tables = "".join(
        person_table(
            pid,
            name,
            role,
            list(dict.fromkeys((pid, *p.looks_after.get(pid, ())))),
            p.emails.get(pid, ""),
            p.zones.get(pid, ""),
        )
        for pid, name, role in p.people
    )
    authority.write_text(authority.read_text(encoding="utf-8") + tables, encoding="utf-8")
    _people_in_books(
        directory, p, parse_policy(tomllib.loads(authority.read_text(encoding="utf-8")))
    )
    _accountable_plan(directory, p, parse_policy(tomllib.loads(authority.read_text("utf-8"))))
    voice = directory / "kit" / "voice.toml"
    _set_line(voice, "spelling", json.dumps(p.spelling))
    if p.banned:
        _set_line(voice, "banned", json.dumps(list(p.banned)))
    # Prove every file the answers touched still loads.
    tenants_root = directory.parent
    load_tenant(p.slug, tenants_root=tenants_root)
    load_kit(directory)
    return OnboardResult(result, run_doctor(p.slug, tenants_root=tenants_root))


__all__ = [
    "OnboardResult",
    "OnboardingError",
    "Plan",
    "Question",
    "apply",
    "load_questions",
    "missing",
    "next_question",
    "people_roles",
    "plan",
    "record",
]
