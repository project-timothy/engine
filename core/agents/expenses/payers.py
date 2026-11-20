"""Who pays a missionary (board chunk c33, t-payer-packet; the owner,
2026-11-12: any missionary, paid by any mix of payers, gets one plain tax
packet in January).

Each missionary keeps a list of payers, each one kind:

- a support platform (EquipNet, Modern Day Missions, a YWAM base account):
  everything it pays them is on a 1099;
- a sending church: their pay on a 1099, their ministry costs reimbursed;
- an agency: a 1099, or a W-2 when it employs them;
- a personal gift: recorded, and kept off the packet (a gift to a person is
  never their income).

A deposit that was the person's own money (``statements.THEIRS_IN``, c31)
matches to its payer by the words the bank prints on it: the payer's own
words when said, else its name. Two payers a deposit could be leave it for
the person. In January the person says what each payer's tax form reports
(``FORM_EVENT``), and the packet checks it against what Tim saw arrive; a
W-2's box 1 wages come with its box 2 federal income tax withheld (c38).

A personal gift that may be pay (c38, gap D): a payer kept as a gift whose
name reads as an organization, or whose deposits come about monthly, is
asked once whether they send it because of the person's ministry work. The
answer is kept on the payer (``work``); a yes is income with no tax form,
counted on Schedule C. A deposit of their own money said to be a gift for
them (``gift_from``) is a gift on that one line, whoever else pays them.

Some of each (t-payer-split, the owner, 2026-11-16): a third answer to the
question, ``work = "some"``, keeps how much of each payment is for their
ministry work as an exact fraction (``share``), worked out by code from the
person's own dollars or percentage. Every deposit from that payer splits
the same way: the part for their ministry work rounds down to the cent and
is pay, and the gift takes the rest, so the two parts always add up to the
deposit. A payer who is all pay or all gift reads exactly as before.

Pure functions, plus the two small jobs that keep what was said.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction

from ...engine.contracts import EventSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from .common import _events_of
from .statements import CAME_IN, PAY_IN, THEIRS_IN, money

PAYER_EVENT = "payers.payer"  # a payer, as said; newest wins per person and name
FORM_EVENT = "payers.form_figure"  # what a payer's tax form reports for a year, as said

PLATFORM, CHURCH, AGENCY, PERSONAL = (
    "support platform",
    "sending church",
    "agency",
    "personal gift",
)
KINDS = (PLATFORM, CHURCH, AGENCY, PERSONAL)
_SAID_AS = {
    "platform": PLATFORM,
    "support platform": PLATFORM,
    "church": CHURCH,
    "sending church": CHURCH,
    "agency": AGENCY,
    "mission agency": AGENCY,
    "gift": PERSONAL,
    "personal gift": PERSONAL,
    "personal": PERSONAL,
}
NEC, W2 = "1099-NEC", "W-2"
WORK_GIFT = "gift for their ministry work"  # a personal gift they said was for their work
SOME_WORK = "gift partly for their ministry work"  # some of each payment for their work
ASK_WORK = "Do they send this because of your ministry work?"

_ORG = re.compile(
    r"\b(church|ministry|ministries|foundation|fund|inc|llc|corp|mission|missions|fellowship|"
    r"assembly|network|chapel|tabernacle|congregation|parish|outreach|society|association|"
    r"trust|international)\b",
    re.I,
)
MONTHLY = range(25, 39)  # days between two deposits that come about monthly


def looks_like_org(name: str) -> bool:
    """Whether a payer's name reads as an organization, not a person."""
    return bool(_ORG.search(name))


def recurs(days: Iterable[str]) -> bool:
    """Whether deposits come on a schedule: three or more in a row, each
    about a month after the last."""
    seen = sorted({date.fromisoformat(d) for d in days})
    run = 1
    for a, b in zip(seen, seen[1:], strict=False):
        run = run + 1 if (b - a).days in MONTHLY else 1
        if run >= 3:
            return True
    return False


def share_of(said: object) -> Fraction | None:
    """A split as kept ("3/10"): part of each payment, more than none and
    less than all; None for anything else."""
    try:
        share = Fraction(str(said or "").strip())
    except (ValueError, ZeroDivisionError):
        return None
    return share if 0 < share < 1 else None


def percent(share: Fraction) -> tuple[str, str]:
    """A split's two parts as percentages to the hundredth, the gift's
    worked out from the first so the two always add up to 100%."""
    work = (Decimal(share.numerator) * 100 / Decimal(share.denominator)).quantize(Decimal("0.01"))

    def said(p: Decimal) -> str:
        return f"{p.normalize():f}%" if p != p.to_integral() else f"{int(p)}%"

    return said(work), said(Decimal(100) - work)


def kind_of(said: str) -> str:
    """The payer kind plain words name, or "" when they name none."""
    return _SAID_AS.get(" ".join(str(said or "").casefold().split()), "")


def _form(kind: str, said: str) -> str:
    """The tax form a payer of ``kind`` sends: a gift sends none; only an
    agency may send a W-2 (it employs the person)."""
    if kind == PERSONAL:
        return ""
    if kind == AGENCY and said.replace(" ", "").upper() in ("W-2", "W2"):
        return W2
    return NEC


@dataclass(frozen=True)
class Payer:
    person: str  # the person's unit (their id, as authority.toml writes it)
    name: str
    kind: str
    words: tuple[str, ...]  # what the bank prints on its deposits; empty = its name
    form: str
    work: str = ""  # a gift's answer to ASK_WORK: "yes", "no", "some", or "" (not asked yet)
    share: Fraction | None = None  # with "some": the part of each payment for their work

    @classmethod
    def of(cls, said: dict) -> Payer:
        kind = str(said.get("kind") or "")
        words = tuple(w.strip() for w in str(said.get("words") or "").split(";") if w.strip())
        work = str(said.get("work") or "")
        share = share_of(said.get("share")) if work == "some" else None
        if kind != PERSONAL or work not in ("yes", "no", "some") or (work == "some" and not share):
            work, share = "", None
        return cls(
            person=str(said.get("person") or ""),
            name=str(said.get("name") or "").strip(),
            kind=kind,
            words=words,
            form=_form(kind, str(said.get("form") or "")),
            work=work,
            share=share,
        )

    @property
    def income(self) -> bool:
        """Whether what it pays is their income, all of it or a part (a gift
        is not, unless it comes for their ministry work)."""
        return self.kind != PERSONAL or self.work in ("yes", "some")

    @property
    def split(self) -> bool:
        """Whether each payment is some pay for their ministry work and some a gift."""
        return self.work == "some"

    @property
    def label(self) -> str:
        if self.kind == PERSONAL and self.work == "yes":
            return WORK_GIFT
        return SOME_WORK if self.split else self.kind

    def parts(self, dollars: str) -> tuple[str, str]:
        """One deposit split as said: the part for their ministry work,
        rounded down to the cent, and the gift, the rest. The two always add
        up to the deposit."""
        cents = int(Decimal(dollars) * 100)
        work = cents * (self.share or 0) // 1
        return money(int(work)), money(cents - int(work))

    def carries(self, text: str) -> int:
        """How long a stretch of ``text`` names this payer (0 = it doesn't)."""
        low = " ".join(text.casefold().split())
        return max((len(w) for w in self.words or (self.name,) if w.casefold() in low), default=0)


def kept(said: Iterable[dict]) -> dict[str, list[Payer]]:
    """Each person's payers, in the order first said; the newest word on a
    payer (by name, whatever its case) stands."""
    out: dict[str, dict[str, Payer]] = {}
    for p in said:
        payer = Payer.of(p)
        if payer.person and payer.name:
            out.setdefault(payer.person, {})[payer.name.casefold()] = payer
    return {person: list(by_name.values()) for person, by_name in out.items()}


def match(words: str, payers: Iterable[Payer]) -> Payer | None:
    """The one payer whose words a deposit carries, or None: none, or two
    as likely, is the person's to say."""
    scored = [(n, p) for p in payers if (n := p.carries(words))]
    if not scored:
        return None
    best = max(n for n, _p in scored)
    top = [p for n, p in scored if n == best]
    return top[0] if len(top) == 1 else None


def _newest_forms(said: Iterable[dict]) -> dict[tuple[str, str, str], dict]:
    return {(str(f["person"]), str(f["payer"]).casefold(), str(f["year"])): f for f in said}


def figures(said: Iterable[dict]) -> dict[tuple[str, str, str], int]:
    """What each payer's tax form reports, in cents (a W-2's box 1 wages),
    keyed by person, the payer's name casefolded, and year; the newest word
    stands."""
    # A $0 form kept before 2026-11-16 carries no cents at all: it said zero.
    return {k: int(f.get("cents") or 0) for k, f in _newest_forms(said).items()}


def withheld(said: Iterable[dict]) -> dict[tuple[str, str, str], int]:
    """A W-2's box 2, federal income tax withheld, in cents, keyed as
    ``figures``; the newest word on the form stands, so one said without it
    has none."""
    return {k: int(f["withheld"]) for k, f in _newest_forms(said).items() if f.get("withheld")}


# ---- a year's deposits, sorted to their payers ------------------------------------------


@dataclass
class Sorted:
    """A person's deposits for a year: what each payer that pays income paid
    (by name), the personal gifts, their own money naming no payer, and the
    questions to ask."""

    paid: dict[str, list[dict]]
    gifts: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    asks: list[dict] = field(default_factory=list)
    books: dict[str, list[dict]] = field(default_factory=dict)  # pay from this ministry's books

    def seen(self, name: str) -> int:
        """What a payer paid: this ministry's own pay lines where it is the
        payer (gap A), else the deposits Tim saw arrive."""
        lines = self.books.get(name, self.paid.get(name, []))
        return sum(int(Decimal(d["dollars"]) * 100) for d in lines)


def sort(rows: Iterable[dict], listed: list[Payer]) -> Sorted:
    """Each deposit to its payer (month_report bank rows of the year): their
    own money to the payer it names, a gift for them said on the line, or
    unmatched; a ministry deposit carrying a payer's name is asked about;
    and a gift that may be pay is asked once (``ASK_WORK``)."""
    out = Sorted(paid={p.name: [] for p in listed if p.income})
    given: dict[str, list[str]] = {}
    for r in rows:
        if not r.get("dollars") or Decimal(r["dollars"]) <= 0:
            continue
        if r.get("role") == PAY_IN:
            continue  # the ministry's own pay arriving: sort_with_pay keeps it (#651)
        payer = match(str(r.get("words") or ""), listed)
        day, dollars = r["date"], r["dollars"]
        if r.get("role") == THEIRS_IN:
            if r.get("gift_from"):  # a gift for them, said on this one line (M04)
                out.gifts.append({"from": r["gift_from"], "date": day, "dollars": dollars})
            elif payer is None:
                out.unmatched.append({"date": day, "dollars": dollars, "words": r["words"]})
            elif not payer.income:
                out.gifts.append({"from": payer.name, "date": day, "dollars": dollars})
                given.setdefault(payer.name, []).append(day)
            elif payer.split:  # some of each: the work part is pay, the rest a gift
                work, gift = payer.parts(dollars)
                out.paid[payer.name].append({"date": day, "dollars": work, "of": dollars})
                out.gifts.append({"from": payer.name, "date": day, "dollars": gift, "of": dollars})
            else:
                out.paid[payer.name].append({"date": day, "dollars": dollars})
        elif r.get("role") == CAME_IN and payer is not None and not r.get("gift_from"):
            was = "a gift for them"
            if payer.income:
                was = "part their pay and part a gift for them" if payer.split else "their pay"
            out.asks.append(
                {
                    "date": day,
                    "dollars": dollars,
                    "payer": payer.name,
                    "ask": f"This deposit carries {payer.name}'s name but counts as the "
                    f"ministry's money. Was it {was}?",
                }
            )
    for p in listed:
        if p.kind == PERSONAL and not p.work:
            if looks_like_org(p.name) or recurs(given.get(p.name, [])):
                out.asks.append({"payer": p.name, "ask": ASK_WORK})
    return out


def sort_with_pay(
    rows: Iterable[dict], listed: list[Payer], pay: tuple[str, list[dict]], person: str
) -> tuple[list[Payer], Sorted]:
    """``sort``, with what this ministry paid the person as pay (``pay`` is
    the ministry's name and ``church_pay.paid_as_pay``): the ministry is
    their payer, by their own word on it where they gave one (its name or
    words carry the ministry's name), else as a sending church that sends a
    1099-NEC. Its figure is the books' pay; a deposit of theirs that carries
    its name, or that is its pay arriving (``PAY_IN``, #651), is the same
    money, kept beside it and never added (gap A)."""
    name, lines = pay
    if not lines:
        return listed, sort(rows, listed)
    own = next(
        (
            p
            for p in listed
            if p.income
            and not p.split  # a sender of some of each is never the ministry itself
            and (p.name.casefold() == name.casefold() or p.carries(name))
        ),
        None,
    )
    if own is None:
        own = Payer(person=person, name=name, kind=CHURCH, words=(), form=NEC)
        listed = [*listed, own]
    rows = list(rows)
    got = sort(rows, listed)
    got.books[own.name] = lines
    got.paid[own.name] += [  # its pay arriving on their own account (#651)
        {"date": r["date"], "dollars": r["dollars"]} for r in rows if r.get("role") == PAY_IN
    ]
    return listed, got


def _usd(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def rows(
    listed: list[Payer], got: Sorted, forms: dict, held: dict, key: tuple[str, str]
) -> tuple[list[dict], list[str]]:
    """The packet's payer rows and what Tim says of each: what it paid that
    Tim saw arrive, checked against what its form says once kept. ``key`` is
    (person, year)."""
    person, year = key
    out, says = [], []
    for p in listed:
        if not p.income:
            continue
        seen = got.seen(p.name)
        row = {
            "name": p.name,
            "kind": p.label,
            "form": p.form,
            "came_in_seen": money(seen),
            "deposits": got.paid[p.name],
        }
        line = f"{p.name} paid {_usd(seen)} that Tim saw arrive."
        if p.split and p.share is not None:
            row["work_share"] = percent(p.share)[0]
            sent = sum(int(Decimal(d["of"]) * 100) for d in got.paid[p.name])
            line = (
                f"{p.name} sent {_usd(sent)} that Tim saw arrive. Of each payment, "
                f"{row['work_share']}, {_usd(seen)} in all, is pay for their ministry work, and "
                "the rest stays a personal gift."
            )
        if p.name in got.books:
            row["paid_as_pay"] = got.books[p.name]
            line = f"{p.name} paid {_usd(seen)} as pay, from its own books."
            if got.paid[p.name]:
                line += (
                    " The deposits of theirs that carry its name are the same money, counted once."
                )
        form = forms.get((person, p.name.casefold(), year))
        if not p.form:
            line += " With no tax form, it counts as income on Schedule C."
        elif form is None:
            line += (
                f" When {p.name}'s {p.form} comes, keep what it says, even if it differs: "
                "the packet checks it."
            )
        elif p.form == W2:
            row["form_says"] = money(form)
            line += f" Its W-2 says wages of {_usd(form)}"
            box2 = held.get((person, p.name.casefold(), year))
            if box2 is not None:
                row["withheld"] = money(box2)
                line += f", with {_usd(box2)} federal income tax withheld"
            line += "."
        else:
            row["form_says"] = money(form)
            if form > seen:
                row["not_seen"] = money(form - seen)
                line += (
                    f" Its {p.form} says {_usd(form)}, so {_usd(form - seen)} went to an "
                    "account Tim doesn't see."
                )
            elif form < seen:
                line += f" Its {p.form} says only {_usd(form)}: check the form before it's filed."
        out.append(row)
        says.append(line)
    return out, says


# ---- the jobs that keep what was said ---------------------------------------------------

_PAYER = ("person", "name", "kind", "words", "form", "work", "share", "by")
_FORM = ("person", "payer", "year", "cents", "withheld", "by")


def keep_said(ctx: JobContext, event: str, fields: tuple[str, ...], what: str) -> JobOutput:
    """One event of what a person said, from the job's params (shared by
    the payer jobs and tax_facts.py)."""
    # A zero is a word said ("the form says $0"); only an absent or empty
    # field stays out (the persona night of 2026-11-16, finding Y02).
    payload = {k: str(ctx.params[k]).strip() for k in fields if ctx.params.get(k) not in (None, "")}
    n = len(_events_of(ctx, event))
    return JobOutput(
        status="ok",
        summary=f"{what}: kept",
        events=[EventSpec(key=f"{event}:{n}", event_type=event, payload=payload)],
    )


def said_key(ctx: JobContext, name: str, event: str, fields: tuple[str, ...]) -> str:
    key = RunKey(ctx, name)
    for f in fields:
        key.param(f)
    key.add("before", len(_events_of(ctx, event)))
    return key.digest()


def payer_key(ctx: JobContext) -> str:
    # A payer with no split keys exactly as it did before splits existed.
    fields = _PAYER if ctx.params.get("share") else tuple(f for f in _PAYER if f != "share")
    return said_key(ctx, "payer", PAYER_EVENT, fields)


def payer_run(ctx: JobContext) -> JobOutput:
    """Keep a payer as said (the door checked its kind)."""
    return keep_said(ctx, PAYER_EVENT, _PAYER, "payer")


def form_key(ctx: JobContext) -> str:
    return said_key(ctx, "payerform", FORM_EVENT, _FORM)


def form_run(ctx: JobContext) -> JobOutput:
    """Keep what a payer's tax form reports for a year (the door read the dollars)."""
    return keep_said(ctx, FORM_EVENT, _FORM, "payer form figure")
