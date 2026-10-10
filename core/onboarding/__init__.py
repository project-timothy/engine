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
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from ..authority import parse_policy
from ..engine.config import load_tenant
from ..engine.doctor import DoctorReport, run_doctor
from ..engine.init import InitResult, init_tenant, render
from ..engine.kit import SHAPES, family_of, load_kit
from .zones import ZoneAnswerError, resolve_zone

QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.toml"

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
        )
        for q in data["question"]
    )


def _question(qid: str) -> Question:
    for q in load_questions():
        if q.id == qid:
            return q
    raise OnboardingError(f"no question {qid!r}")


@cache
def people_roles(shape: str) -> tuple[str, ...]:
    """The roles a person can hold in ``shape``: the shape's defaults, minus
    the roles only its agents hold."""
    files = render("preview", "A", shape=shape, data_root_rel="preview-data")
    policy = parse_policy(tomllib.loads(files["authority.toml"]))
    agent_roles = {r for a in policy.agents.values() for r in a.roles}
    return tuple(r for r in policy.roles if r not in agent_roles)


def _shape(answers: dict) -> str | None:
    shape = answers.get("who")
    return shape if shape in SHAPES else None


def next_question(answers: dict) -> dict | None:
    """The next unanswered question for this shape, ready for an agent to ask:
    id, ask, help, kind, choices, default, optional. None when done."""
    shape = _shape(answers)
    for q in load_questions():
        if q.id in answers or not q.applies_to(shape):
            continue
        if shape is None and q.id != "who":
            continue
        out: dict = {"id": q.id, "ask": q.ask, "kind": q.kind, "optional": q.optional}
        if q.help:
            out["help"] = q.help
        if q.kind == "choice":
            out["choices"] = [c["label"] for c in q.choices]
        if q.kind in ("role", "people") and shape:
            out["choices"] = list(people_roles(shape))
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


def _people(qid: str, value: object, shape: str) -> list[dict]:
    roles = people_roles(shape)
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
        people.append(person)
    return people


def record(answers: dict, qid: str, value: object) -> dict:
    """``answers`` with ``qid`` answered; refuses an answer the question
    cannot take. The first answer must be ``who``."""
    q = _question(qid)
    shape = _shape(answers)
    if qid != "who" and shape is None:
        raise OnboardingError("answer who this is for first")
    text = value.strip() if isinstance(value, str) else value
    if q.kind == "choice":
        wanted = str(text).casefold()
        for choice in q.choices:
            if wanted in (choice["label"].casefold(), str(choice["value"]).casefold()):
                return {**answers, qid: choice["value"]}
        raise OnboardingError(
            f"{qid}: {value!r} is not one of {', '.join(c['label'] for c in q.choices)}"
        )
    if q.kind == "text":
        if not text and not q.optional:
            raise OnboardingError(f"{qid}: needs an answer")
        return {**answers, qid: str(text or "")}
    if q.kind == "person":
        if not text:
            return {**answers, qid: ""}
        name, email = name_and_email(qid, str(text))
        return {**answers, qid: {"name": name, "email": email} if email else name}
    if q.kind == "month":
        return {**answers, qid: _month(qid, text)}
    if q.kind == "timezone":
        try:
            return {**answers, qid: resolve_zone(str(text or q.default_for(shape) or ""))}
        except ZoneAnswerError as exc:
            raise OnboardingError(f"{qid}: {exc}") from exc
    if q.kind == "amount":
        return {**answers, qid: _amount(qid, text)}
    if q.kind == "list":
        return {**answers, qid: _items(text) if text else []}
    assert shape is not None
    if q.kind == "role":
        return {**answers, qid: _role(qid, str(text), people_roles(shape))}
    if q.kind == "people":
        return {**answers, qid: _people(qid, text, shape) if text else []}
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
    banned: tuple[str, ...]
    spelling: str
    emails: dict[str, str] = field(default_factory=dict)  # person id -> address, if given


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

    def get(qid: str) -> object:
        if qid in answers:
            return answers[qid]
        return _question(qid).default_for(shape)

    taken: set[str] = set()
    people: list[tuple[str, str, str]] = []
    emails: dict[str, str] = {}
    you = str(answers["your_name"])
    your_role = SELF_ROLE.get(shape) or str(answers["your_role"])
    people.append((_person_id(you, taken), you, your_role))
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
    for entry in answers.get("people", []) or []:
        pid = _person_id(entry["name"], taken)
        people.append((pid, entry["name"], entry["role"]))
        if entry.get("email"):
            emails[pid] = entry["email"]
    family = family_of(shape)
    entity = str(get("entity") if family == "commercial" else get("entity_nonprofit")) or ""
    size = shape.split("-", 1)[1]
    return Plan(
        slug=slug,
        shape=shape,
        archetype=str(get("work")) if family == "commercial" else "C",
        legal_name=str(answers["legal_name"]),
        timezone=str(get("timezone")),
        fiscal_start=int(str(get("fiscal_start"))),
        entity=entity,
        people=tuple(people),
        self_approval_limit=str(get("second_person_above")) if size == "small" else None,
        two_approvals_above=str(get("two_approvals_above")) if size != "solo" else None,
        banned=tuple(_items(answers.get("banned_words", []) or [])),
        spelling=str(get("spelling")),
        emails=emails,
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


TEMPLATE_PEOPLE = ("Pat Owner", "Sam Staffer", "Vic Vendor")
"""The placeholder people ``engine init`` renders for an owner to replace."""


def _files_expenses(policy, role: str) -> bool:
    return any(g.action in ("submit", "*") for g in policy.roles[role].grants)


def _people_in_books(directory: Path, p: Plan, policy) -> None:
    """Put the answered people where the template has placeholders: who files
    expenses (anyone whose role may submit; an owner books as owner, everyone
    else as a reimbursed person), whose phone photos the inbox assumes (the
    first of them, the person answering), and the owners the close treats as
    owners (none in a nonprofit). Walkthrough 1 found Pat Owner, Sam Staffer
    and Vic Vendor filing a missionary's expenses."""
    filers = [(name, role) for _pid, name, role in p.people if _files_expenses(policy, role)]
    booked = [(name, "owner" if role == "owner" else "employee") for name, role in filers]
    rows = "".join(
        f"    {{name = {json.dumps(name)}, role = {json.dumps(kind)}}},\n" for name, kind in booked
    )
    tenant = directory / "tenant.toml"
    text = tenant.read_text(encoding="utf-8")
    text, count = re.subn(r"(?ms)^persons = \[\n.*?^\]$", f"persons = [\n{rows}]", text)
    if count != 1:
        raise OnboardingError("tenant.toml: expected one persons list")
    tenant.write_text(text, encoding="utf-8")
    _set_line(tenant, "inbox_person", json.dumps(filers[0][0] if filers else ""))
    owners = [name for _pid, name, role in p.people if role == "owner"]
    _set_line(tenant, "owner_names", json.dumps(owners))
    cfg = load_tenant(p.slug, tenants_root=directory.parent)
    drop = Path(cfg.expenses.drop_dir)
    if cfg.expenses.drop_dir and drop.is_dir():
        for name, _role in filers:
            (drop / name).mkdir(exist_ok=True)
        for name in TEMPLATE_PEOPLE:
            folder = drop / name
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()


def apply(
    p: Plan,
    *,
    root: str | Path | None = None,
    data_root: str | Path | None = None,
    ledger_dir: str | Path | None = None,
    run_audit: bool = True,
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
    if p.entity:
        _set_line(directory / "tenant.toml", "entity", json.dumps(p.entity))
    authority = directory / "authority.toml"
    if p.self_approval_limit is not None:
        _set_line(authority, "self_approval_limit", p.self_approval_limit)
        _set_line(authority, "distinct_payer_above", p.self_approval_limit)
    if p.two_approvals_above is not None:
        _set_line(authority, "second_approver_above", p.two_approvals_above)
    # Each person's own id is their unit, so an @own grant covers their own
    # submissions; the agent widens a scope later when the tenant says so.
    # An address the person gave rides beside them, for the sign-in invitation.
    tables = "".join(
        f"\n[people.{pid}]\n# {json.dumps(name)[1:-1]}\n"
        f"roles = [{json.dumps(role)}]\nscopes = [{json.dumps(pid)}]\n"
        + (f"email = {json.dumps(p.emails[pid])}\n" if pid in p.emails else "")
        for pid, name, role in p.people
    )
    authority.write_text(authority.read_text(encoding="utf-8") + tables, encoding="utf-8")
    _people_in_books(
        directory, p, parse_policy(tomllib.loads(authority.read_text(encoding="utf-8")))
    )
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
