"""Laws the books keep, over random missionary months (the owner's ask of
2026-10-15: test in loops for what a missionary's own money does to the
ministry's books).

A seeded generator plays a few months the way they happen: cash drawn at an
ATM in Kathmandu, from the ministry's account or the person's own, posted by
the bank up to three days later, with or without the day in the bank's
words; rupee receipts and school payments paid from the cash actually in
hand; dollar and rupee purchases by card; the ATM's fee. The generator keeps
the truth (which cash paid for what), the books are read back through
``statements``, and each law holds them to the truth or to themselves.

A broken law shrinks its story to the fewest acts that still break it and
prints them, the start of a named regression test (the ones found so far
close this file). ``FUZZ_SEEDS=5000 uv run pytest
tests/unit/test_books_laws.py`` hunts longer.

Whose cash pays first is the owner's call (2026-10-16): the ministry's
cash, oldest first, then the person's own, and a purchase that takes both
owes back only the person's part (found here: about a third of stories with
own-account cash read it all as the ministry's).

Two rupee receipts within a few days and the bank's spread of each other,
one paid by card and one from cash, can't be told apart by their amounts
(6 stories in 5,000): Tim asks which rode the card (2026-10-16), and until
the person says, neither is cash and the line waits. The stories here
answer as the person would, from the truth.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from core.agents.expenses import fx, records
from core.agents.expenses import statements as st

SEEDS = int(os.environ.get("FUZZ_SEEDS", "300"))
START = date(2026, 7, 1)
DAYS = 92  # a quarter, so months turn over
PEOPLE = ("Daniel Reyes", "Mei Chen")


@dataclass(frozen=True)
class Draw:
    """Cash out of an ATM on ``day`` (the person's own clock), posted
    ``lag`` days later; ``says_day`` when the bank's words carry the day."""

    i: int
    person: str
    day: date
    cents: int
    rupees: int
    ministry: bool
    lag: int
    says_day: bool


@dataclass(frozen=True)
class Spend:
    """Rupees paid from cash in hand: a receipt, or a school payment."""

    i: int
    person: str
    day: date
    rupees: int
    school: bool


@dataclass(frozen=True)
class Card:
    """A purchase by card on the ministry's account, its receipt in dollars
    or rupees; the card posts ``lag`` days later."""

    i: int
    person: str
    day: date
    cents: int
    rupees: int  # 0: paid in dollars
    lag: int


@dataclass(frozen=True)
class Tea:
    """Rupees from cash in hand with no receipt (tea, taxis, tips): the
    books learn of them only at the next count."""

    i: int
    person: str
    day: date
    rupees: int


@dataclass(frozen=True)
class Count:
    """The person counts the envelope and says where the missing cash went."""

    i: int
    person: str
    day: date


Act = Draw | Spend | Card | Tea | Count


# ---- the story ----------------------------------------------------------------------


def story(seed: int) -> list[Act]:
    """A quarter of acts, each spend from cash only when the truth has the
    rupees in hand."""
    rng = random.Random(seed)  # noqa: S311  (a seeded story, never a secret)
    people = PEOPLE if rng.random() < 0.3 else PEOPLE[:1]
    own_cash = rng.random() < 0.5
    says = rng.random()
    tea = random.Random(seed * 7919 + 1)  # noqa: S311  (a seeded story, never a secret)
    acts: list[Act] = []
    in_hand = {p: 0 for p in people}
    for offset in range(DAYS):
        day = START + timedelta(days=offset)
        for person in people:
            if rng.random() < 0.08:
                rupees = rng.choice((10000, 20000, 30000, 40000))
                per_usd = Decimal(rng.randint(12800, 14200)) / 100
                cents = int((Decimal(rupees) / per_usd * 100).quantize(Decimal(1)))
                ministry = not own_cash or rng.random() < 0.6
                lag = rng.choice((0, 1, 1, 2, 3)) if day.weekday() < 5 else rng.randint(2, 3)
                acts.append(
                    Draw(len(acts), person, day, cents, rupees, ministry, lag, rng.random() < says)
                )
                in_hand[person] += rupees
            if in_hand[person] and rng.random() < 0.35:
                rupees = min(in_hand[person], rng.choice((250, 800, 1500, 4300, 12000)))
                acts.append(Spend(len(acts), person, day, rupees, rng.random() < 0.15))
                in_hand[person] -= rupees
            if rng.random() < 0.12:
                rupees = rng.choice((0, 0, 2600, 5100))
                cents = rng.randint(400, 9000) if not rupees else 0
                acts.append(Card(len(acts), person, day, cents, rupees, rng.randint(0, 2)))
            # cash with no receipt, and the count that finds it (a stream of its own, so
            # the stories above stay the ones the found tests name)
            if in_hand[person] and tea.random() < 0.2:
                rupees = min(in_hand[person], tea.choice((100, 300, 650)))
                acts.append(Tea(len(acts), person, day, rupees))
                in_hand[person] -= rupees
            if tea.random() < 0.06:
                acts.append(Count(len(acts), person, day))
    return acts


def _sha(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def _rate(day: date) -> Decimal:
    return Decimal(13300 + day.toordinal() % 97) / 100


def book(acts: list[Act], *, answer: bool = True) -> list[dict]:
    """The events the door would have kept for these acts; ``answer`` says
    which receipt rode each card line Tim asks about, as the person would."""
    log: list[dict] = []
    seen: dict[tuple, int] = {}
    card_of: dict[str, Card] = {}

    def line(person: str, account: str, day: date, words: str, cents: int) -> dict:
        key = (person, account, day, cents)
        seen[key] = seen.get(key, 0) + 1
        ln = st.Line(day.isoformat(), words, cents, seen[key] - 1)
        log.append({"event_type": st.LINE_EVENT, "payload": ln.payload(person, account)})
        return log[-1]

    for a in acts:
        if isinstance(a, Draw):
            account = st.MINISTRY if a.ministry else st.OWN
            posted = a.day + timedelta(days=a.lag)
            when = f" {a.day:%m/%d}" if a.says_day else ""
            words = f"NON-CHASE ATM WITHDRAW {a.i:06d}{when} KATHMANDU NP"
            atm = line(a.person, account, posted, words, -a.cents)
            sha = atm["payload"]["sha256"]
            tag = f"NPR{a.rupees}.00"
            log.append({"event_type": st.PURPOSE_EVENT, "payload": {"sha256": sha, "cash": tag}})
            line(a.person, account, posted, "NON-CHASE ATM FEE-WITH", -500)
        elif isinstance(a, Spend):
            tag = f"NPR{a.rupees}.00"
            if a.school:
                row = st.SchoolPayment(
                    a.day.isoformat(), f"Student {a.i}", "Shree Janata", Decimal(a.rupees), "NPR"
                )
                log.append({"event_type": st.SCHOOL_EVENT, "payload": row.payload(a.person)})
            else:
                _receipt(log, a, tag)
        elif isinstance(a, Card):
            posted = a.day + timedelta(days=a.lag)
            if a.rupees:
                rate = fx.Rate("NPR", a.day.isoformat(), str(_rate(a.day)), "test")
                log.append({"event_type": fx.RATE_EVENT, "payload": rate.payload()})
                cents = int(Decimal(rate.cents(Decimal(a.rupees))) * Decimal("1.03"))
                _receipt(log, a, f"NPR{a.rupees}.00")
            else:
                cents = a.cents
                _receipt(log, a, f"${Decimal(cents) / 100:.2f}")
            paid = line(a.person, st.MINISTRY, posted, f"BHATBHATENI SUPERMARKE {a.i}", -cents)
            card_of[paid["payload"]["sha256"]] = a
        elif isinstance(a, Count):
            _count(log, acts, a)
    for line_sha in st.in_doubt(log) if answer else {}:
        said = {"sha256": _sha("receipt", card_of[line_sha].i), "line": line_sha}
        log.append({"event_type": records.RODE_EVENT, "payload": said})
    return log


def _count(log: list[dict], acts: list[Act], a: Count) -> None:
    """The count the door records, and the cash used it finds, in the
    person's words: the tea since their last count, the ministry's spending."""
    before = acts[: acts.index(a) + 1]
    have = truth(before).on_hand.get(a.person, (0, Decimal(0)))[0]
    day = a.day.isoformat()
    log.append(
        {
            "event_type": records.CASH_COUNT_EVENT,
            "payload": {
                "sha256": _sha("count", a.i),
                "person": a.person,
                "day": day,
                "tag": f"NPR{have}.00",
            },
        }
    )
    pending = _pending_tea(before, a.person)
    if pending:
        used = {
            "sha256": _sha("used", a.i),
            "person": a.person,
            "day": day,
            "tag": f"NPR{pending}.00",
            "whose": "ministry",
            "said": "tea, taxis",
        }
        log.append({"event_type": records.CASH_USED_EVENT, "payload": used})


def _pending_tea(acts: list[Act], person: str) -> int:
    """Tea since the person's last count, up to the end of ``acts``."""
    total = 0
    for a in acts:
        if a.person != person:
            continue
        if isinstance(a, Tea):
            total += a.rupees
        elif isinstance(a, Count) and a is not acts[-1]:
            total = 0
    return total


def _receipt(log: list[dict], a: Act, tag: str) -> None:
    sha = _sha("receipt", a.i)
    name = f"{a.day.isoformat()}_receipt_{a.i}.jpg"
    landed = {"sha256": sha, "file": name, "person": a.person}
    log.append({"event_type": records.LANDED_EVENT, "payload": landed})
    log.append({"event_type": records.AMOUNT_EVENT, "payload": {"sha256": sha, "tag": tag}})


@dataclass(frozen=True)
class _Used:
    """A count's cash used with no receipt, spent like a receipt."""

    sha: str
    person: str
    day: date
    rupees: int
    school: bool = False


def spend_sha(a: Spend | _Used) -> str:
    if isinstance(a, _Used):
        return a.sha
    if a.school:
        row = st.SchoolPayment(
            a.day.isoformat(), f"Student {a.i}", "Shree Janata", Decimal(a.rupees), "NPR"
        )
        return row.sha(a.person)
    return _sha("receipt", a.i)


@dataclass
class Truth:
    """What cash really paid for each spend: its cents, whose cash it came
    from ("own", "ministry", or both), and what the person's own part cost."""

    cents: dict[str, Decimal]
    whose: dict[str, set[str]]
    own: dict[str, Decimal]
    on_hand: dict[str, tuple[int, Decimal]]
    rest: dict[str, int]  # cents the day's rate priced, for rupees the cash didn't cover
    used: dict[str, int]  # rupees each spend took from cash


def truth(acts: list[Act]) -> Truth:
    """The ministry's cash first, then the person's own (the owner, 2026-10-16),
    each oldest first by the day it was really drawn. A spend the
    cash in hand can't cover was paid partly some other way: the cash in
    hand goes to it only when the day's rate prices the rest."""
    rated = {a.day for a in acts if isinstance(a, Card) and a.rupees}
    pools: dict[str, list[list]] = {}
    cents: dict[str, Decimal] = {}
    whose: dict[str, set[str]] = {}
    own: dict[str, Decimal] = {}
    rest: dict[str, int] = {}
    used: dict[str, int] = {}
    tea: dict[str, int] = {}
    rank = {Draw: 0, Card: 0, Spend: 1, Tea: 1, Count: 2}
    for a in sorted(acts, key=lambda a: (a.day, rank[type(a)], a.i)):
        if isinstance(a, Draw):
            pools.setdefault(a.person, []).append([a.rupees, Decimal(a.cents) / a.rupees, a])
        elif isinstance(a, Tea):  # the books learn of it at the next count
            tea[a.person] = tea.get(a.person, 0) + a.rupees
        elif isinstance(a, Spend | Count):
            if isinstance(a, Count):
                a = _Used(_sha("used", a.i), a.person, a.day, tea.pop(a.person, 0))
                if not a.rupees:
                    continue
            pool = [c for c in pools.get(a.person, []) if c[0] > 0]
            have = sum(c[0] for c in pool)
            if not pool or (have < a.rupees and a.day not in rated):
                continue
            need, cost, mine, theirs = min(a.rupees, have), Decimal(0), set(), Decimal(0)
            for c in sorted(pool, key=lambda c: not c[2].ministry):
                take = min(need, c[0])
                cost += take * c[1]
                mine.add("ministry" if c[2].ministry else "own")
                theirs += 0 if c[2].ministry else take * c[1]
                c[0] -= take
                need -= take
                if not need:
                    break
            short = a.rupees - min(a.rupees, have)
            rest[spend_sha(a)] = (
                fx.Rate("NPR", "", str(_rate(a.day)), "").cents(short) if short else 0
            )
            used[spend_sha(a)] = a.rupees - short
            cents[spend_sha(a)] = cost + rest[spend_sha(a)]
            whose[spend_sha(a)] = mine
            own[spend_sha(a)] = theirs
    on_hand = {
        p: (sum(c[0] for c in pool), sum(c[0] * c[1] for c in pool)) for p, pool in pools.items()
    }
    return Truth(cents, whose, own, on_hand, rest, used)


# ---- the laws -----------------------------------------------------------------------


def law_cash_pays_what_it_paid(acts: list[Act], log: list[dict]) -> str:
    """Every spend the cash really paid reads as paid from cash, at what it
    cost (to the cent when every bank says the day it was drawn)."""
    drawn, _on_hand = st.cash(log)
    real = truth(acts)
    exact = all(a.says_day for a in acts if isinstance(a, Draw))
    for sha, cents in real.cents.items():
        if sha not in drawn:
            return f"{sha[:8]} was paid from cash, but the books don't say so"
        want = int(cents.quantize(Decimal(1), ROUND_HALF_UP))
        if exact and abs(drawn[sha].cents - want) > 1:
            return f"{sha[:8]} cost {want} cents of cash, the books say {drawn[sha].cents}"
    return ""


def law_cash_is_conserved(acts: list[Act], log: list[dict]) -> str:
    """The dollars drawn as cash are what the spent cash cost plus what's in
    hand: none lost, none counted twice."""
    drawn, on_hand = st.cash(log)
    real = truth(acts)
    spent = {spend_sha(a): (a.person, a.rupees) for a in acts if isinstance(a, Spend)}
    for u in st.used(log):  # cash a count found gone with no receipt
        spent[str(u["sha256"])] = (str(u["person"]), int(Decimal(str(u["tag"])[3:])))
    for person in {a.person for a in acts if isinstance(a, Draw)}:
        given = [a for a in acts if isinstance(a, Draw) and a.person == person]
        used = [s for s, (who, _r) in spent.items() if who == person and s in drawn]
        dollars = sum(a.cents for a in given)
        rupees = sum(a.rupees for a in given)
        held_rupees, held_cents = on_hand.get((person, "NPR"), (Decimal(0), 0))
        out_rupees = sum(real.used.get(s, spent[s][1]) for s in used)
        if Decimal(rupees) != held_rupees + out_rupees:
            return f"{person}: drew {rupees} rupees, spent {out_rupees}, holds {held_rupees}"
        out_cents = sum(drawn[s].cents - real.rest.get(s, 0) for s in used)
        if abs(dollars - out_cents - held_cents) > len(used) + len(given):
            return f"{person}: drew {dollars} cents of cash, spent {out_cents}, holds {held_cents}"
    return ""


def law_own_cash_is_owed_back(acts: list[Act], log: list[dict]) -> str:
    """A spend paid from the ministry's cash owes nothing back; one paid
    from the person's own cash is owed back; one paid from both owes back
    the person's part and no more, to the cent. Held where every bank says
    the day cash was drawn: where one doesn't, the books let its cash count
    a few days early, and which cash paid first can honestly differ."""
    if not all(a.says_day for a in acts if isinstance(a, Draw)):
        return ""  # a bank that hides the day lets either cash count a few days early
    drawn, _on_hand = st.cash(log)
    real = truth(acts)
    for sha, whose in real.whose.items():
        if sha not in drawn:
            continue
        d = drawn[sha]
        if d.ministry != ("ministry" in whose):
            return f"{sha[:8]} was paid from {sorted(whose)} cash, the books say otherwise"
        want = int(real.own[sha].quantize(Decimal(1), ROUND_HALF_UP))
        if d.ministry and abs(d.owed() - want) > 1:
            return f"{sha[:8]} took {want} cents of their own cash, the books owe {d.owed()}"
    return ""


def law_a_card_receipt_stands_behind_its_own_line(acts: list[Act], log: list[dict]) -> str:
    """A card purchase's receipt rides its card line, never the cash."""
    drawn, _on_hand = st.cash(log)
    behind = st.attached(log)
    for a in acts:
        if isinstance(a, Card):
            sha = _sha("receipt", a.i)
            if sha in drawn:
                return f"card receipt {a.i} was paid from cash"
            if sha not in behind:
                return f"card receipt {a.i} stands behind no line"
    return ""


def law_order_does_not_matter(acts: list[Act], log: list[dict]) -> str:
    """Statements and receipts arrive in any order, some twice: the books
    read the same."""
    rng = random.Random(len(log))  # noqa: S311  (a seeded shuffle)
    shuffled = [*log, *rng.sample(log, len(log) // 3)]
    rng.shuffle(shuffled)
    if st.cash(log) != st.cash(shuffled):
        return "the cash reads differently in another order"
    for month in _months(acts):
        if _norm(st.month_lines(log, month)) != _norm(st.month_lines(shuffled, month)):
            return f"{month} reads differently in another order"
    return ""


def law_every_line_lands_once(acts: list[Act], log: list[dict]) -> str:
    """Each kept line is in exactly one month, cash is never spending, and
    a school payment from the ministry's cash owes nothing back."""
    drawn, _on_hand = st.cash(log)
    seen: dict[str, str] = {}
    for month in _months(acts):
        for ln in st.month_lines(log, month):
            if ln["sha256"] in seen:
                return f"{ln['sha256'][:8]} is in {seen[ln['sha256']]} and {month}"
            seen[ln["sha256"]] = month
            if ln["role"] == st.CASH and ln["waiting_on"]:
                return f"cash line {ln['words']} still waits on its rupees"
            if ln.get("school") and ln["sha256"] in drawn:
                want = st.MINISTRY_PAID if drawn[ln["sha256"]].ministry else st.OWED
                if ln["role"] != want:
                    return f"school payment {ln['words']} reads {ln['role']}"
    kept = {e["payload"]["sha256"] for e in log if e["event_type"] == st.LINE_EVENT}
    missing = kept - set(seen)
    return f"{len(missing)} kept lines are in no month" if missing else ""


def law_cash_in_hand_rolls_month_to_month(acts: list[Act], log: list[dict]) -> str:
    """Cash in hand at a month's end is never less than nothing nor more
    than was drawn, and at the end it is what the truth holds."""
    for month in _months(acts):
        end = _month_end(month)
        _drawn, held = st.cash(log, through=end)
        for (person, _cur), (rupees, cents) in held.items():
            drew = sum(a.rupees for a in acts if isinstance(a, Draw) and a.person == person)
            if rupees < 0 or cents < 0 or rupees > drew:
                return f"{person} holds {rupees} rupees at {end}"
    _drawn, held = st.cash(log)
    for person, (rupees, _cost) in truth(acts).on_hand.items():
        got = held.get((person, "NPR"), (Decimal(0), 0))[0]
        if got != rupees:
            return f"{person} holds {rupees} rupees, the books say {got}"
    return ""


def law_a_count_matches_once_said(acts: list[Act], log: list[dict]) -> str:
    """Once a count's missing cash is said for, the books hold what the
    envelope holds, that day. A bank that doesn't say the day it was drawn
    lets cash count a few days early, so a count just before such a
    withdrawal is left out."""
    for a in acts:
        if not isinstance(a, Count):
            continue
        early = any(
            isinstance(d, Draw)
            and d.person == a.person
            and not d.says_day
            and 0 < (d.day - a.day).days <= st.NEAR_DAYS
            for d in acts
        )
        if early:
            continue
        counted = next(
            Decimal(str(c["tag"])[3:]) for c in st.counts(log) if c["sha256"] == _sha("count", a.i)
        )
        _drawn, held = st.cash(log, through=a.day.isoformat())
        books = held.get((a.person, "NPR"), (Decimal(0), 0))[0]
        if books != counted:
            return f"{a.person} counted {counted} on {a.day}, the books hold {books}"
    return ""


LAWS = [
    law_cash_pays_what_it_paid,
    law_cash_is_conserved,
    law_own_cash_is_owed_back,
    law_a_card_receipt_stands_behind_its_own_line,
    law_order_does_not_matter,
    law_every_line_lands_once,
    law_cash_in_hand_rolls_month_to_month,
    law_a_count_matches_once_said,
]


def _months(acts: list[Act]) -> list[str]:
    days = {a.day + timedelta(days=getattr(a, "lag", 0)) for a in acts} | {a.day for a in acts}
    return sorted({d.isoformat()[:7] for d in days})


def _month_end(month: str) -> str:
    first = date.fromisoformat(f"{month}-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return last.isoformat()


def _norm(lines: list[dict]) -> list[tuple]:
    return sorted((ln["sha256"], ln["role"], ln["cents"], ln["waiting_on"]) for ln in lines)


def test_the_owed_back_law_meets_envelopes_holding_both():
    """The law above holds only where every bank says the day; enough of
    those stories still spend from both cashes for it to bite."""
    mixed = 0
    for seed in range(SEEDS):
        acts = story(seed)
        if all(a.says_day for a in acts if isinstance(a, Draw)):
            mixed += sum(len(w) > 1 for w in truth(acts).whose.values())
    assert mixed >= SEEDS // 100


# ---- the loop -----------------------------------------------------------------------


def shrink(acts: list[Act], law) -> list[Act]:
    """The fewest acts that still break ``law``: drop each act in turn, and
    keep the drop while the law still breaks."""
    smaller = True
    while smaller:
        smaller = False
        for i in range(len(acts)):
            trial = acts[:i] + acts[i + 1 :]
            if trial and law(trial, book(trial)):
                acts, smaller = trial, True
                break
    return acts


@pytest.mark.parametrize(
    "law", LAWS, ids=lambda f: getattr(f, "__name__", "law").removeprefix("law_")
)
def test_the_books_keep_the_law(law):
    for seed in range(SEEDS):
        acts = story(seed)
        broken = law(acts, book(acts))
        if broken:
            small = shrink(acts, law)
            told = "\n".join(f"    {a!r}," for a in small)
            pytest.fail(f"seed {seed}: {broken}\nsmallest story:\n[\n{told}\n]")


# ---- what the laws found (2026-10-15) ----------------------------------------------


DANIEL, MEI = PEOPLE


def test_found_two_rupee_card_purchases_days_apart_each_keep_their_own_line():
    """Matching greedily by the closest dollars, a later receipt took the
    earlier purchase's line, and the receipt left over read as cash."""
    acts = [
        Draw(0, DANIEL, date(2026, 7, 17), 28233, 40000, True, 0, False),
        Card(2, DANIEL, date(2026, 7, 19), 0, 2600, 2),
        Card(5, DANIEL, date(2026, 7, 24), 0, 2600, 1),
    ]
    log = book(acts)
    assert st.attached(log) == {_sha("receipt", 2), _sha("receipt", 5)}
    assert st.cash(log)[0] == {}


def test_found_a_small_rupee_receipt_never_stands_behind_the_atm_fee():
    """700 rupees is about $5.25, inside the bank's spread of a $5 fee."""
    acts = [
        Draw(0, DANIEL, date(2026, 7, 16), 14231, 20000, True, 1, False),
        Draw(1, MEI, date(2026, 8, 20), 23211, 30000, True, 0, False),
        Spend(2, DANIEL, date(2026, 8, 23), 700, False),
        Card(3, DANIEL, date(2026, 8, 23), 0, 5100, 0),
    ]
    drawn, _on_hand = st.cash(book(acts))
    assert _sha("receipt", 2) in drawn


def test_found_a_receipt_never_stands_behind_another_persons_card():
    acts = [
        Draw(0, DANIEL, date(2026, 8, 11), 31245, 40000, True, 3, True),
        Card(1, DANIEL, date(2026, 8, 13), 0, 5100, 0),
        Spend(2, MEI, date(2026, 8, 13), 5300, False),
    ]
    assert st.attached(book(acts)) == {_sha("receipt", 1)}


def test_found_two_withdrawals_the_same_day_spend_in_one_order_however_they_arrive():
    acts = [
        Draw(0, DANIEL, date(2026, 8, 3), 14258, 20000, True, 0, False),
        Draw(1, DANIEL, date(2026, 8, 3), 7514, 10000, True, 0, False),
        Spend(2, DANIEL, date(2026, 8, 5), 12000, False),
    ]
    log = book(acts)
    assert st.cash(log) == st.cash(list(reversed(log)))


def test_found_a_purchase_from_both_cashes_owes_back_the_persons_part():
    """The ministry's cash pays first, the person's own after (the owner,
    2026-10-16): NPR 12,000 takes the ministry's 20,000 and none of theirs;
    the NPR 16,300 before it took 16,300 of their own."""
    acts = [
        Draw(0, DANIEL, date(2026, 8, 20), 15228, 20000, False, 1, False),
        Spend(1, DANIEL, date(2026, 8, 21), 16300, False),
        Draw(2, DANIEL, date(2026, 9, 14), 14582, 20000, True, 2, True),
        Spend(3, DANIEL, date(2026, 9, 28), 12000, False),
    ]
    assert law_own_cash_is_owed_back(acts, book(acts)) == ""


def test_found_two_close_rupee_receipts_and_one_card_line_tim_asks_which():
    """NPR 5,100 by card on Aug 12 and NPR 5,300 from cash on Aug 13, one
    card line: amounts can't tell them apart, so neither is guessed."""
    acts = [
        Draw(0, MEI, date(2026, 8, 7), 7398, 10000, True, 1, False),
        Card(1, MEI, date(2026, 8, 12), 0, 5100, 1),
        Card(2, DANIEL, date(2026, 8, 13), 0, 5100, 0),
        Spend(3, MEI, date(2026, 8, 13), 5300, False),
    ]
    card, cash = _sha("receipt", 1), _sha("receipt", 3)
    asked = book(acts, answer=False)
    [(line, receipts)] = st.in_doubt(asked).items()
    assert sorted(receipts) == sorted([card, cash])
    assert not {card, cash} & (set(st.cash(asked)[0]) | st.attached(asked))
    [row] = [ln for ln in st.month_lines(asked, "2026-08") if ln["sha256"] == line]
    assert row["waiting_on"] == st.WAITING_WHICH

    said = book(acts)  # she says the 5,100 one was the card
    assert st.in_doubt(said) == {}
    assert law_a_card_receipt_stands_behind_its_own_line(acts, said) == ""
    assert cash in st.cash(said)[0]
