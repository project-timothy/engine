"""A person's month and year of receipts: the month report (its lines,
drives, bank lines, cash in hand, and what the rules say) and the year
report. Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from ..agents.expenses import advances, budget, fx, records, statements
from ..agents.expenses.categories import UNCATEGORIZED
from ..agents.expenses.job_send import HELD_EVENT
from ..agents.expenses.records import (
    AMOUNT_EVENT,
    CATEGORY_EVENT,
    DATED_EVENT,
    DRIVE_EVENT,
    LANDED_EVENT,
    PRICED_EVENT,
    PROPOSED_EVENT,
    PURPOSE_EVENT,
    READ_EVENT,
    REFERENCE_EVENT,
    REVIEW_CARD,
    SEND_CARD,
    SENT_EVENT,
    drive_id,
)
from ..agents.expenses.report_lines import amounts_said, claim_name
from ..agents.expenses.schema import parse_amount_tag
from ..agents.expenses.statements import money
from ..agents.policy.jobs import clock_in_force
from ..policy import Clock
from .reads import _advances, _events, _log
from .receipts import from_tag
from .rules import _rulebook
from .viewer import display_name, unit_of

if TYPE_CHECKING:
    from .catalog import Tools


def _said(line: dict) -> str | None:
    """A foreign amount the way the person said it, with its code."""
    if not line.get("said_currency"):
        return None
    return f"{line['said_currency']} {Decimal(line['said_amount']):,.2f}"


def _listed_in_dollars(line: dict) -> str | None:
    """A school payment listed in dollars, as listed: said, so an empty field
    never reads as an amount missing (the dress rehearsal, 2026-10-15)."""
    if not line.get("school") or line.get("cents") is None:
        return None
    return f"${money(line['cents'])}"


def _rate_words(line: dict) -> str | None:
    if not line.get("fx_rate"):
        return None
    per = f"{Decimal(line['fx_rate']):,.2f}"
    return f"{line['said_currency']} {per} to the dollar on {line['fx_date']}"


def _month_line(line: dict, source: str) -> dict:
    cents = line.get("amount_cents")
    return {
        "date": line.get("date") or None,
        "said": _said(line),
        "dollars": money(cents) if cents is not None else None,
        "rate": _rate_words(line),
        "rate_from": line.get("fx_source") or None,
        "category": line.get("category") or UNCATEGORIZED,
        "fund": line.get("project") or None,
        "read": line.get("read") or None,
        "waiting_on": "the day's exchange rate"
        if cents is None and line.get("said_currency")
        else line.get("unpriced") or None,
        "source": source,
    }


@dataclass
class _Sending:
    """Where each receipt stands with the person's institution, for a
    ministry whose claims go there (the send job's cards and events): sent,
    held, going on the next run, waiting for a yes, or not sent yet."""

    t: Tools
    who: str
    states: dict[str, tuple[str, str]]
    cards: dict[str, str]

    @classmethod
    def read(cls, t: Tools) -> _Sending | None:
        rows = t._rows(
            "SELECT status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
            "ORDER BY id",
            (t.tenant, SEND_CARD),
        )
        if not rows:
            return None
        cards, who = {}, ""
        for r in rows:
            params = json.loads(r["params_json"] or "{}")
            who = str(params.get("institution") or who)
            if r["status"] in ("pending", "approved"):
                cards.update(dict.fromkeys(str(params.get("receipts", "")).split(","), r["status"]))
        states = {
            str(e.get("receipt")): (e["event_type"], str(e.get("say", "")))
            for _i, e in _events(t, SENT_EVENT, HELD_EVENT)
        }
        return cls(t, who or "the institution", states, cards)

    def _yes(self, person: str) -> str:
        mine = self.t.viewer is not None and self.t.viewer.person == unit_of(person)
        return "your yes" if mine else f"{person}'s yes"

    def where(self, sha: str, person: str) -> str:
        kind, say = self.states.get(sha, ("", ""))
        if kind == SENT_EVENT:
            return f"Sent to {self.who}"
        if kind == HELD_EVENT:
            return say
        if self.cards.get(sha) == "approved":
            return f"Going to {self.who} on the next run"
        if self.cards.get(sha) == "pending":
            return f"Waiting for {self._yes(person)}"
        return "Not sent yet"

    def review(self, person: str) -> str:
        return f"Goes to {self.who} on {self._yes(person)}"


def _amounts(t: Tools) -> tuple[dict[str, str], dict[str, dict]]:
    """Door receipts' amounts after the photo: said by the person (a tag),
    and read off the photo (a proposal: its tag, and who it reads as paid
    and for what, when the reading said)."""
    logged = [
        {"event_type": e["event_type"], "payload": e}
        for _i, e in _events(t, AMOUNT_EVENT, READ_EVENT)
    ]
    readings = {
        str(e["payload"]["sha256"]): e["payload"]
        for e in logged
        if e["event_type"] == READ_EVENT and e["payload"].get("sha256")
    }
    return amounts_said(logged), readings


def _said_name(said: dict[str, str], sha: str, name: str) -> str:
    return claim_name(name, said[sha]) if sha in said else name


def _unread(sha: str, name: str, cents: int | None, readings: dict[str, dict]) -> dict:
    """What a receipt sent without an amount is waiting on, and what its
    photo reads when it was read ("$18.75 at Shoprite Maputo, Meals")."""
    if cents is not None or not ("_receipt_unread" in name or records.UNDATED in name):
        return {}
    if sha not in readings:
        return {"unpriced": "how much it was"}
    reading = readings[sha]
    words = from_tag(str(reading["tag"])).words()
    if reading.get("payee"):
        words += f" at {reading['payee']}"
    if reading.get("category"):
        words += f", {reading['category']}"
    return {"read": words, "unpriced": "their word on the amount"}


def _month_drives(t: Tools, month: str) -> dict[str, list[dict]]:
    """Each person's drives in the month (core/mileage): miles, what the
    person said it was for, and its dollars at the rate on its own day when
    it was the ministry's. A drive nobody spoke for waits on their word."""
    from ..agents.expenses import drives
    from ..engine.config import load_tenant

    said = {str(e.get("drive")): e for _i, e in _events(t, PURPOSE_EVENT)}
    settings = load_tenant(t.tenant).institution
    filed = {
        str(r["receipt_sha256"])
        for r in t._rows("SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,))
    }
    out: dict[str, list[dict]] = {}
    for eid, e in _events(t, DRIVE_EVENT):
        day = str(e.get("start", ""))[:10]
        if day[:7] != month or records.drive_sha(drive_id(e)) in filed:
            continue  # a filed drive is a line of the month's report
        word = said.get(drive_id(e), {})
        out.setdefault(str(e.get("person")), []).append(
            drives.line(e, word, settings, day, f"events#{eid}")
        )
    return out


def _records(t: Tools) -> dict[str, records.Record]:
    """Where each receipt stands, read from the books (records.where)."""
    events = [
        {
            "event_type": r["event_type"],
            "created_at": r["created_at"],
            "payload": json.loads(r["payload_json"] or "{}"),
        }
        for r in t._rows(
            "SELECT event_type, payload_json, created_at FROM events WHERE tenant = ? ORDER BY id",
            (t.tenant,),
        )
    ]
    cards = [
        {**dict(r), "params": json.loads(r["params_json"] or "{}")}
        for r in t._rows(
            "SELECT id, action_type, status, params_json, created_at, resolved_at "
            "FROM approval_queue WHERE tenant = ? AND action_type IN (?, ?) ORDER BY id",
            (t.tenant, REVIEW_CARD, SEND_CARD),
        )
    ]
    lines = t._rows(
        "SELECT l.receipt_sha256, r.status FROM expense_line l "
        "JOIN expense_report r ON r.id = l.report_id WHERE r.tenant = ? AND r.shadow = 0",
        (t.tenant,),
    )
    return records.where(events, cards, [dict(r) for r in lines])


def _review_yours(t: Tools, month: str) -> dict[str, bool]:
    """For each person whose month waits on a review card: whether the one
    asking would decide it with their own yes (the Haiku run, A09: Tim sent
    Eli to his board member when his ministry lets him approve his own).
    Empty where the box's door decides nothing."""
    if t.decider is None or not t.decider.offered():
        return {}
    yours: dict[str, bool] = {}
    for r in t._rows(
        "SELECT id, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status = 'pending' ORDER BY id",
        (t.tenant, REVIEW_CARD),
    ):
        params = json.loads(r["params_json"] or "{}")
        if params.get("month") == month and params.get("person"):
            yours[params["person"]] = t.decider.may_tap(t, int(r["id"]))
    return yours


def _review_stands(t: Tools, month: str) -> dict[str, str]:
    """Where each person's month stands with its reviewer: a card waiting,
    approved, or the report's own status once built."""
    stands: dict[str, str] = {}
    for r in t._rows(
        "SELECT id, status, params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status IN ('pending', 'approved') ORDER BY id",
        (t.tenant, REVIEW_CARD),
    ):
        params = json.loads(r["params_json"] or "{}")
        if params.get("month") == month:
            stands[params.get("person")] = (
                "Waiting for approval" if r["status"] == "pending" else "Approved"
            )
    for r in t._rows(
        "SELECT person, status FROM expense_report WHERE tenant = ? AND month = ? AND shadow = 0",
        (t.tenant, month),
    ):
        stands[r["person"]] = r["status"]
    return stands


def _rules_say(t: Tools, month: str) -> dict[str, list[str]]:
    """What the ministry's rules in force say about each person's month, as
    their newest review card carries it (core/agents/policy: the card is the
    one place it is worked out, so the card, the page and Tim agree)."""
    said: dict[str, list[str]] = {}
    for r in t._rows(
        "SELECT params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status IN ('pending', 'approved') ORDER BY id",
        (t.tenant, REVIEW_CARD),
    ):
        params = json.loads(r["params_json"] or "{}")
        if params.get("month") == month and "policy" in params:
            words = str(params["policy"])
            said[str(params.get("person"))] = [] if words == "none" else words.split("; ")
    return said


def _with_rules_say(people: list[dict], said: dict[str, list[str]]) -> list[dict]:
    """Each person's month with what the rules say, only where they say
    something: a ministry with no rules in force reads as it always did."""
    return [p | {"rules_say": said[p["person"]]} if said.get(p["person"]) else p for p in people]


def _month_report(t: Tools, args: dict) -> dict:
    """The missionary's month (alpha step 3): each person's receipts for the
    month, a foreign one shown as said beside its dollar value at the rate
    on its own date, the dollar total, and where the review stands. Read
    only: a rate not yet recorded shows as waiting, never guessed."""
    month = str(args.get("month") or t._today().strftime("%Y-%m"))
    who = str(args.get("person") or "").strip()
    rates = fx.stored(
        [{"event_type": e["event_type"], "payload": e} for _i, e in _events(t, fx.RATE_EVENT)]
    )
    people: dict[str, list[dict]] = {}

    filed = t._rows(
        "SELECT l.*, r.person FROM expense_line l JOIN expense_report r ON r.id = l.report_id "
        "WHERE r.tenant = ? AND r.month = ? AND r.shadow = 0 ORDER BY l.id",
        (t.tenant, month),
    )
    consumed = {
        str(r["receipt_sha256"])
        for r in t._rows("SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,))
    }
    stands = _records(t)
    where = stands  # every record's stage; ``stands`` is reused below for the reviews
    log = _log(t)
    banked = {  # a filed statement line or school payment reads once, as a bank line
        str(e["payload"].get("sha256"))
        for e in log
        if e["event_type"] in (statements.LINE_EVENT, statements.SCHOOL_EVENT)
    }
    for r in filed:
        if str(r["receipt_sha256"]) in banked:
            continue
        line = dict(r)
        line["date"] = line["fx_date"] or line["expense_date"] or str(line["receipt_file"])[:10]
        shown = _month_line(line, f"expense_line#{r['id']}")
        rec = stands.get(str(r["receipt_sha256"]))
        shown["stands"] = rec.stage if rec else "reported"
        people.setdefault(r["person"], []).append(shown)

    read: dict[str, int] = {}
    placed: dict[str, str] = {}
    for _i, e in _events(t, PROPOSED_EVENT, PRICED_EVENT):
        if e.get("amount_cents") is not None:
            read[str(e.get("sha256"))] = int(e["amount_cents"])
        if e.get("category"):
            placed[str(e.get("sha256"))] = str(e["category"])
    for _i, e in _events(t, CATEGORY_EVENT):
        placed[str(e.get("sha256"))] = str(e.get("category", ""))
    skip = (
        consumed
        | {str(e.get("sha256")) for _i, e in _events(t, REFERENCE_EVENT)}
        | statements.held(log)  # behind a bank line, or one Tim asks about: no line of its own
    )
    sending = _Sending.read(t)
    own_pay = records.from_pay(log)
    drawn, on_hand = statements.cash(log, through=f"{month}-31")
    said, readings = _amounts(t)
    dated = {str(e.get("sha256")): str(e.get("day")) for _i, e in _events(t, DATED_EVENT)}
    for eid, e in _events(t, LANDED_EVENT):
        sha = str(e.get("sha256", ""))
        sent_as = str(e.get("file", ""))
        name = _said_name(said, sha, records.dated_name(sent_as, dated.get(sha, "")))
        if (dated[sha][:7] if sha in dated else str(e.get("month"))) != month or sha in skip:
            continue
        skip.add(sha)
        undated = records.UNDATED in sent_as and sha not in dated
        line = {
            "date": name[:10] if name[:4].isdigit() and not undated else None,
            "category": placed.get(sha, ""),
            "project": e.get("project", ""),
        }
        line.update(fx.priced(name, rates, drawn.get(sha)))
        if "amount_cents" not in line:
            line["amount_cents"] = read.get(sha, parse_amount_tag(name))
        line.update(_unread(sha, name, line["amount_cents"], readings))
        if undated:
            line["unpriced"] = "the receipt's day"
        shown = _month_line(line, f"events#{eid}")
        shown["stands"] = stands[sha].stage if sha in stands else "captured"
        shown.update(_paid_from(drawn.get(sha), own_pay=sha in own_pay))
        if sending is not None:
            shown["sent"] = sending.where(sha, str(e.get("person")))
        people.setdefault(str(e.get("person")), []).append(shown)

    stands = _review_stands(t, month)
    from .decide import who_approves  # decide reaches catalog through the door

    yours = _review_yours(t, month)

    drives = _month_drives(t, month)
    bank = _month_bank(t, month, log, consumed, where)
    for person in [*drives, *bank]:
        people.setdefault(person, [])
    out = []
    for person, lines in sorted(people.items()):
        if who and who.casefold() not in person.casefold():
            continue
        if not t.sees("expense.report", unit_of(person)):
            continue
        if len({ln["fund"] for ln in lines if ln["fund"]}) < 2:
            for ln in lines:  # one fund is never named on every line (10/14)
                ln["fund"] = None
        priced = [ln for ln in lines if ln["dollars"] is not None]
        mine = bank.get(person, [])
        owed = [b for b in mine if b["role"] == statements.OWED and b["dollars"] is not None]
        theirs = [ln for ln in priced if not ln.get("paid")]
        total = sum(int(Decimal(ln["dollars"]) * 100) for ln in [*theirs, *owed])
        # their own cash in what the ministry's cash paid first: only that part is owed back
        total += sum(
            int(Decimal(ln["owed_back"]) * 100) for ln in [*priced, *mine] if ln.get("owed_back")
        )
        spending = [
            b for b in mine if b["role"] in statements.SPENDING and b["dollars"] is not None
        ]
        ministry = [ln for ln in priced if ln.get("paid") != OWN_PAY_SAYS]
        spent = sum(int(Decimal(ln["dollars"]) * 100) for ln in [*ministry, *spending])
        in_hand = _in_hand(on_hand, person, cash_when(month, t._today()))
        out.append(
            {
                "person": person,
                "month": month,
                "spent": money(spent),
                "total": money(total),
                "receipts": len(lines),
                "waiting_on_a_rate": len(lines) - len(priced),
                "review": sending.review(person)
                if sending is not None
                else stands.get(person, "Not yet sent for review"),
                **(
                    {
                        "review_yours": yours[person],
                        "review_who_approves": who_approves(yours[person]),
                    }
                    if person in yours
                    else {}
                ),
                "lines": lines,
                "drives": drives.get(person, []),
                "bank": mine,
                "bank_waiting": sum(1 for b in mine if b["waiting_on"]),
                **({"cash_on_hand": "; ".join(in_hand)} if in_hand else {}),
                **_cash_more(t, log, month, person, mine),
            }
        )
    return {"month": month, "people": _with_rules_say(out, _rules_say(t, month))}


OWN_PAY_SAYS = "from their own pay: nobody owes it back, and it counts on their tax packet"


def _paid_from(cash: fx.Drawn | None, *, own_pay: bool = False) -> dict:
    """A receipt paid from the ministry's own ATM cash: nothing owed back,
    or only the person's own part when their cash paid the rest (the owner,
    2026-10-16). One they paid from their own pay is owed to nobody (c34 gap C)."""
    if own_pay:
        return {"paid": OWN_PAY_SAYS}
    if cash is None or not cash.ministry:
        return {}
    if not cash.own_cents:
        return {"paid": "from the ministry's cash"}
    owed = money(cash.own_cents)
    return {"paid": f"from the ministry's cash and ${owed} of your own", "owed_back": owed}


def _cash_more(t: Tools, log: list[dict], month: str, person: str, bank: list[dict]) -> dict:
    """What else the month says about the person's cash: what they owe the
    ministry, their last count, and cash nobody has accounted for in 45
    days or more (t-cash-count, t-advance-clock)."""
    out: dict = {}
    repay = sum(int(Decimal(b["dollars"]) * 100) for b in bank if b["role"] == statements.REPAY)
    if repay:
        out["owed_to_ministry"] = money(repay)
    counted = [
        c for c in statements.counts(log) if c["person"] == person and str(c["day"])[:7] == month
    ]
    if counted:
        out["cash_counted"] = f"{statements.cash_words(counted[-1]['tag'])} on {counted[-1]['day']}"
    asof = min(statements.month_end(month), t._today().isoformat())
    plan = _clock(t, person)
    for (whose, cur), (day, left) in sorted(statements.cash_age(log, asof).items()):
        old = statements.days_old(day, asof)
        if whose != person or old < plan.nudge:
            continue
        due = (
            f"past {plan.due} days: account for it or return it"
            if old > plan.due
            else f"due at {plan.due} days"
        )
        out["cash_oldest"] = (
            f"{statements.cash_words(f'{cur}{left}')} drawn {day}, {old} days ago ({due})"
        )
    return out


def _clock(t: Tools, person: str) -> Clock:
    """The cash clock for this person: an adopted plan's days, else Tim's."""
    con = t._db()
    try:
        policy = t.viewer.policy if t.viewer is not None else None
        return clock_in_force(con, t.tenant, _rulebook(t), policy, person)
    finally:
        con.close()


def cash_when(month: str, today: date) -> str:
    """When the month's cash in hand is as of: its last day, or today in the
    month still running."""
    if month >= today.strftime("%Y-%m"):
        return "today"
    return f"at the end of {date.fromisoformat(month + '-01'):%B}"


def _in_hand(on_hand: dict, person: str, when: str) -> list[str]:
    """The person's ATM cash not yet spent, when, and what it cost (t-atm-cash)."""
    return [
        f"{statements.cash_words(f'{cur}{rupees}')} {when} (cost ${money(cents)})"
        for (whose, cur), (rupees, cents) in sorted(on_hand.items())
        if whose == person
    ]


def _month_bank(
    t: Tools, month: str, log: list[dict], reported: set[str], stands: dict
) -> dict[str, list[dict]]:
    """Each person's statement lines and school payments in the month
    (t-statements): what each is, its dollars, whether a receipt stands
    behind it, and what it still waits on. Read through the same code as
    the month's card (statements.month_lines)."""
    from ..engine.config import load_tenant

    listed = list(load_tenant(t.tenant).expenses.category_accounts)
    # A receipt riding its line is named by its own source, so the person's
    # AI can still split it (the dress rehearsal, 2026-10-15).
    landed = {str(e.get("sha256")): eid for eid, e in _events(t, LANDED_EVENT)}
    doubt = statements.in_doubt(log)
    said = {
        r: (day, f"{cur} {amount:,.2f}") for r, day, cur, amount in statements.foreign_receipts(log)
    }
    out: dict[str, list[dict]] = {}
    for ln in statements.month_lines(log, month, reported=reported, categories=listed):
        rec = stands.get(ln["sha256"])
        behind = landed.get(str(ln["receipt"] or ""))
        out.setdefault(ln["person"], []).append(
            {
                "date": ln["day"],
                "words": ln["words"],
                "dollars": money(ln["cents"]) if ln["cents"] is not None else None,
                "role": ln["role"],
                "account": ln["account"] or None,
                "said": _said(ln) or _listed_in_dollars(ln),
                "rate": _rate_words(ln),
                "category": ln["category"] or None,
                "receipt": "attached" if ln["receipt"] else None,
                **({"receipt_source": f"events#{behind}"} if behind is not None else {}),
                "for": ln["said"] or None,
                **({"gave": ln["gave"]} if ln.get("gave") else {}),
                **({"part": ln["part"]} if ln.get("part") else {}),
                **({"owed_back": money(ln["owed_cents"])} if ln.get("owed_cents") else {}),
                **(
                    {"could_be": _could_be(doubt[ln["sha256"]], landed, said)}
                    if ln["sha256"] in doubt
                    else {}
                ),
                "waiting_on": ln["waiting_on"] or None,
                "stands": rec.stage if rec else "captured",
                **_told(ln),
                "source": f"bank#{ln['sha256'][:16]}",
                "paid_from": ln.get("paid_from") or None,
                **({"paid_to": ln["paid_to"]} if ln.get("paid_to") else {}),
                **{
                    k: ln[k]
                    for k in ("gift_from", "fund_grant", "for_fund", "processor")
                    if ln.get(k)
                },
                **_names(ln),
            }
        )
    for person, rows in advances.month_rows(_advances(t), month).items():
        out.setdefault(person, []).extend(rows)  # paid it themselves, approved (c28)
    return out


def _names(ln: dict) -> dict:
    """A deposit whose words name a person, until it's said whose it is: Tim
    asks whether it was their pay (c31, the EquipNet lesson, 2026-11-12)."""
    if ln["role"] != statements.CAME_IN or ln["said"]:
        return {}
    named = statements.names_a_person(str(ln["words"]))
    return {"names": named} if named else {}


def _told(ln: dict) -> dict:
    """Who told Tim a line's words, by name, and when (persona night S05)."""
    if not ln.get("told_at"):
        return {}
    by = str(ln.get("told_by") or "")
    return {"told_by": display_name(by) if by else None, "told_at": ln["told_at"]}


def _could_be(shas: list[str], landed: dict, said: dict) -> list[dict]:
    """The receipts a card line might have carried, as Tim asks it: oldest first."""
    rows = []
    for sha in shas:
        day, amount = said.get(sha, ("", ""))
        rows.append({"source": f"events#{landed.get(sha, '')}", "date": day, "said": amount})
    return sorted(rows, key=lambda r: (r["date"], r["said"]))


def _year_report(t: Tools, args: dict) -> dict:
    """Where's my year: each month's receipts, total, what still waits, and
    where its review stands; totals by category; and receipts sent in a
    catch-up that are in no month yet. Read only."""
    year = str(args.get("year") or t._today().year)
    who = str(args.get("person") or "").strip()
    people: dict[str, dict] = {}
    for m in range(1, 13):
        month = f"{year}-{m:02d}"
        if month > t._today().strftime("%Y-%m"):
            break
        for p in _month_report(t, {"month": month, "person": who})["people"]:
            if not p["lines"] and not p["drives"] and not p["bank"]:
                continue
            me = people.setdefault(p["person"], {"person": p["person"], "months": [], "cats": {}})
            me["months"].append(
                {
                    "month": month,
                    "receipts": p["receipts"],
                    "bank_lines": len(p["bank"]),
                    "spent": p["spent"],
                    "owed_back": p["total"],
                    "waiting": p["waiting_on_a_rate"] + p["bank_waiting"],
                    "review": p["review"],
                }
            )
            _add(me["cats"], _spent_by_kind(p))
    dated = {str(e.get("sha256")) for _i, e in _events(t, DATED_EVENT)}
    filed = {
        str(r["receipt_sha256"])
        for r in t._rows("SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (t.tenant,))
    }
    for _i, e in _events(t, LANDED_EVENT):
        person, sha = str(e.get("person", "")), str(e.get("sha256", ""))
        if records.UNDATED not in str(e.get("file", "")) or sha in dated | filed:
            continue
        if (who and who.casefold() not in person.casefold()) or not t.sees(
            "expense.report", unit_of(person)
        ):
            continue
        me = people.setdefault(person, {"person": person, "months": [], "cats": {}})
        me["undated"] = me.get("undated", 0) + 1
    out = []
    for me in sorted(people.values(), key=lambda p: p["person"]):
        out.append(
            {
                "person": me["person"],
                "months": me["months"],
                "by_category": {k: money(v) for k, v in sorted(me["cats"].items())},
                "not_yet_in_a_month": me.get("undated", 0),
            }
        )
    report: dict = {"year": year, "people": out}
    set_for = budget.latest((e for _i, e in _events(t, budget.BUDGET_EVENT)), year)
    if set_for:
        spent: dict[str, int] = {}
        for me in people.values():
            for kind, cents in me["cats"].items():
                spent[kind] = spent.get(kind, 0) + cents
        report["budget"] = budget.standing(set_for, spent, year=year, today=t._today())
    return report


def _spent_by_kind(p: dict) -> dict[str, int]:
    """A person's month of spending by kind, in cents: receipts, and the bank
    lines that are spending: school payments, card buys a receipt rides, cash
    with no receipt (the dress rehearsal, 2026-10-15: the scholarship budget
    read untouched); and pay under the board's resolution, the ministry's
    spending under its Pay kind (c34; 2026-11-15, the export didn't tie
    without it). The year report and the books add up the same lines."""
    out: dict[str, int] = {}
    spent = [ln for ln in p["bank"] if ln["role"] in (*statements.SPENDING, statements.PAY_OUT)]
    for line in [*p["lines"], *spent]:
        if line["dollars"] is not None:
            kind = line["category"] or "Uncategorized"
            cents = int(Decimal(line["dollars"]) * 100)
            pay = line.get("role") == statements.PAY_OUT  # a line out, read as money out
            out[kind] = out.get(kind, 0) + (abs(cents) if pay else cents)
    return out


def _add(into: dict[str, int], more: dict[str, int]) -> None:
    for kind, cents in more.items():
        into[kind] = into.get(kind, 0) + cents
