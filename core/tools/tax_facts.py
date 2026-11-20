"""What only the person can tell Tim for their taxes, at the door (board
chunk c38; the facts themselves: core/agents/expenses/tax_facts.py).

Two tools that register themselves (the 11/15 plan, rule 9):

- ``keep_tax_facts``: filing status, a spouse's wages and federal tax
  withheld as one pair, last year's total tax, an estimated payment and its
  day, and who else may read their packet.
- ``keep_travel``: a trip, the day they left the United States, the day
  they came back, and the country.

The missionary says these, or whoever looks over every month says them for
the missionary they name; the same rule decides who reads their tax packet,
plus anyone the missionary named (the owner, 2026-11-15, after decision 2:
the president does the missionary's books). Code reads every number and day;
nothing is guessed.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..agents.expenses import tax_facts as facts_of
from ..agents.expenses.statements import money
from .catalog import Tools, ToolSpec, _events, _schema, _sees_every_month, register
from .viewer import display_name, filers, unit_of

_ACTS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _cents(said: object) -> int | None:
    """Dollars as said ("$1,250.50", "1250") in cents, or None."""
    text = str(said or "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if amount < 0 or amount != amount.quantize(Decimal("0.01")):
        return None
    return int(amount * 100)


def _day(said: object) -> date | None:
    try:
        return date.fromisoformat(str(said or "").strip())
    except ValueError:
        return None


def _words(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def _dollars(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def _files(t: Tools) -> bool:
    """Whether the person asking is a missionary with a packet of their own."""
    return t.viewer is not None and t.viewer.person in filers(t.viewer.policy)


def _offered(t: Tools) -> bool:
    # the box's door: a missionary, or whoever looks over every month (the owner, 2026-11-15)
    return t.receipts is not None and (_files(t) or _sees_every_month(t))


def _keep(t: Tools, job: str, params: dict) -> dict | None:
    from ..engine.runner import door_run as run

    done = run(
        t.tenant,
        "expenses",
        job,
        params={**params, "by": t.viewer.person},
        ledger_dir=Path(t.receipts.ledger_root).parent,
        tenants_root=t.receipts.tenants_root,
    )
    if done.status == "error":
        return {"status": "busy", "say": "Tim couldn't keep that just now. Try again soon."}
    return None


def _said(t: Tools, event: str) -> list[dict]:
    return [e for _i, e in _events(t, event)]


# ---- keep_tax_facts ---------------------------------------------------------------------


def _year(t: Tools, args: dict) -> str | None:
    """The tax year said (this year when left out); next year is allowed,
    for an estimate paid ahead."""
    year = str(args.get("year") or t._today().year).strip()
    return year if re.fullmatch(r"\d{4}", year) and int(year) <= t._today().year + 1 else None


def _share(t: Tools, said: str, unit: str) -> tuple[str, str] | dict:
    """Who to share the packet with: (units joined by ;, their names) or a reply."""
    if " ".join(said.casefold().split()) in ("no one", "nobody", "none", "stop", "stop sharing"):
        return facts_of.NO_ONE, "no one"
    whose_unit = unit
    names = [n.strip() for n in re.split(r",|;| and ", said) if n.strip()]
    units = [unit_of(n) for n in names]
    listed = set(t.viewer.policy.people)
    for name, unit in zip(names, units, strict=True):
        if unit not in listed:
            return {
                "status": "needs",
                "say": f"{name} isn't on your ministry's list. Tim shares your packet only "
                "with someone on it; your preparer gets the PDF from you.",
            }
        if unit == whose_unit:
            return {"status": "needs", "say": "Your packet is already yours. Who else?"}
    return ";".join(units), " and ".join(display_name(u) for u in units)


def _last_year(args: dict, fact: dict, done: list[str]) -> dict | None:
    """Last year's total tax into ``fact``, or the question to ask."""
    if args.get("last_year_tax") in (None, ""):
        return None
    c = _cents(args["last_year_tax"])
    if c is None:
        return {"status": "needs", "say": "Say last year's total tax in dollars, like 4,210."}
    fact["last_year_tax"] = c
    done.append(f"last year's total tax of {_dollars(c)}")
    return None


def _support(args: dict, fact: dict, done: list[str]) -> dict | None:
    """Whether support reaches them where Tim doesn't see it (none, or
    some) into ``fact``, or the question to ask (persona night Y07)."""
    said = str(args.get("support_elsewhere") or "").strip().casefold()
    if not said:
        return None
    if said not in ("none", "some"):
        return {
            "status": "needs",
            "say": "Does any support for their work reach them where Tim doesn't see it? "
            "Pass none, or some.",
        }
    fact["support_elsewhere"] = said
    done.append(
        "that all the support for your work is what Tim sees"
        if said == "none"
        else "that some support reaches you where Tim doesn't see it (keep each one who "
        "sends it with keep_payers)"
    )
    return None


def _keep_tax_facts(t: Tools, args: dict) -> dict:
    """Keep a missionary's own tax facts for a year, in their own words (c38)."""
    unit, stop = whose(t, args)
    if stop is not None:
        return stop
    year = _year(t, args)
    if year is None:
        return {"status": "needs", "say": "Say which tax year, like 2026."}
    fact: dict = {"person": unit, "year": year}
    done: list[str] = []
    kept = facts_of.facts(_said(t, facts_of.FACT_EVENT), unit, year)

    said_status = str(args.get("status") or "").strip()
    status = kept.status
    if said_status:
        status = facts_of.status_of(said_status)
        if status == "other":
            return {
                "status": "needs",
                "say": "Tim works out single, or married filing jointly. For any other "
                "filing status your preparer works out the tax. Which of those two is it?",
            }
        if not status:
            return {
                "status": "needs",
                "say": "Are you filing single, or married filing jointly?",
            }
        fact["status"] = status
        done.append(f"filing {facts_of.STATUS_WORDS[status]} for {year}")

    wages, withheld = args.get("spouse_wages"), args.get("spouse_withheld")
    if wages not in (None, "") or withheld not in (None, ""):
        w, h = _cents(wages), _cents(withheld)
        if w is None or h is None:
            return {
                "status": "needs",
                "say": "Say your spouse's wages and the federal tax withheld from them "
                "together, as their W-2 shows them (boxes 1 and 2). Say 0 if none was withheld.",
            }
        if status != facts_of.JOINT:
            return {
                "status": "needs",
                "say": "A spouse's wages count only on a joint return. Are you married "
                "filing jointly?",
            }
        fact |= {"spouse_wages": w, "spouse_withheld": h}
        done.append(f"your spouse's {year} wages of {_dollars(w)}, {_dollars(h)} withheld")

    stop = _last_year(args, fact, done) or _support(args, fact, done)
    if stop is not None:
        return stop

    if args.get("estimate_paid") not in (None, "") or args.get("estimate_day"):
        c, day = _cents(args.get("estimate_paid")), _day(args.get("estimate_day"))
        if c is None or day is None:
            return {
                "status": "needs",
                "say": "Say the estimated payment's dollars and the day you paid it.",
            }
        if day > t._today():
            return {"status": "needs", "say": "Tim keeps estimates already paid. Which day?"}
        fact |= {"estimate_cents": c, "estimate_day": day.isoformat()}
        done.append(f"an estimated payment of {_dollars(c)} on {_words(day)}, toward {year}")

    if str(args.get("share_with") or "").strip():
        shared = _share(t, str(args["share_with"]), unit)
        if isinstance(shared, dict):
            return shared
        fact["shared_with"], names = shared
        done.append(
            "your packet kept to you alone"
            if names == "no one"
            else f"your packet shared with {names}"
        )

    if not done:
        return {
            "status": "needs",
            "say": "Say what to keep: your filing status, your spouse's wages and tax "
            "withheld, last year's total tax, or an estimated payment and its day.",
        }
    stop = _keep(t, "tax_fact", fact)
    if stop is not None:
        return stop
    return {"status": "set", "say": "Done: Tim has " + "; ".join(done) + "."}


KEEP_TAX_FACTS = register(
    ToolSpec(
        "keep_tax_facts",
        "Keep the missionary's own tax facts for a year, as they say them: their filing "
        "status (single, or married filing jointly), a spouse's W-2 wages and federal tax "
        "withheld (always the two together, joint returns only), last year's total tax, an "
        "estimated payment with the day they paid it (estimate_paid, estimate_day as "
        "YYYY-MM-DD), whether any support for their work reaches them where Tim doesn't "
        "see it (support_elsewhere: none, or some), and who else may read their tax packet "
        "(share_with: names on the ministry's list, or no one). Say only what they said; "
        "leave the rest out. The newest word stands. The missionary tells Tim these, or "
        "whoever looks over every month does, naming them in person. It moves no money.",
        _schema(
            {
                "year": {"type": "string", "description": "The tax year, like 2026."},
                "status": {
                    "type": "string",
                    "description": "single, or married filing jointly.",
                },
                "spouse_wages": {"type": "string", "description": "W-2 box 1, like 41,200."},
                "spouse_withheld": {"type": "string", "description": "W-2 box 2, like 3,100."},
                "last_year_tax": {"type": "string", "description": "Last year's total tax."},
                "estimate_paid": {"type": "string", "description": "An estimate's dollars."},
                "estimate_day": {"type": "string", "description": "The day paid, YYYY-MM-DD."},
                "support_elsewhere": {
                    "type": "string",
                    "enum": ["none", "some"],
                    "description": "none: all their support is what Tim sees; some: more "
                    "reaches them elsewhere.",
                },
                "share_with": {"type": "string", "description": "Names, or no one."},
                "person": {"type": "string", "description": "The missionary's name."},
            }
        ),
        _keep_tax_facts,
        annotations=_ACTS,
        short=(
            "Keep the missionary's own tax facts for a year, as they say them: status (single, or"
            " married filing jointly), a spouse's W-2 wages and withheld (always the two "
            "together, joint returns only), last_year_tax, an estimate paid with its day "
            "(estimate_paid, estimate_day YYYY-MM-DD), support_elsewhere (none, or some: support "
            "for their work that reaches them where Tim doesn't see it), and share_with (names on"
            " the ministry's list who may read their packet, or no one). Pass only what they "
            "said. The newest word stands. person: the missionary, when their reviewer says it."
        ),
    ),
    _offered,
)
"""A missionary's tax facts (c38): the box's door, the person and their reviewer."""


# ---- keep_travel ------------------------------------------------------------------------


def _keep_travel(t: Tools, args: dict) -> dict:
    """Keep one trip a missionary took, in their own words (c38)."""
    unit, stop = whose(t, args)
    if stop is not None:
        return stop
    left = _day(args.get("left"))
    if left is None:
        return {
            "status": "needs",
            "say": "Say the day you left the United States, and the day you came back.",
        }
    if args.get("forget"):
        stop = _keep(t, "tax_travel", {"person": unit, "left": left.isoformat(), "forget": "yes"})
        return stop or {
            "status": "set",
            "say": f"Done: Tim forgot the trip that left on {_words(left)}.",
        }
    back = _day(args.get("back")) if str(args.get("back") or "").strip() else None
    if str(args.get("back") or "").strip() and back is None:
        return {"status": "needs", "say": "Say the day you came back, like 2026-06-01."}
    country = " ".join(str(args.get("country") or "").split())
    if not country:
        return {"status": "needs", "say": "Which country were you in?"}
    today = t._today()
    if left > today or (back is not None and back > today):
        return {"status": "needs", "say": "Tim keeps trips already taken. Which days?"}
    if back is not None and back < left:
        return {"status": "needs", "say": "The day back comes after the day you left. Which days?"}
    end = back or today
    for trip in facts_of.trips(_said(t, facts_of.TRAVEL_EVENT), unit):
        if trip.left == left.isoformat():
            continue  # said again: this one replaces it
        other_end = date.fromisoformat(trip.back) if trip.back else today
        if date.fromisoformat(trip.left) <= end and left <= other_end:
            return {
                "status": "needs",
                "say": f"That overlaps the trip to {trip.country} that left on "
                f"{_words(date.fromisoformat(trip.left))}. Which one is right?",
            }
    stop = _keep(
        t,
        "tax_travel",
        {
            "person": unit,
            "left": left.isoformat(),
            "back": back.isoformat() if back else "",
            "country": country,
        },
    )
    if stop is not None:
        return stop
    came = f"came back on {_words(back)}" if back else "are still there"
    return {
        "status": "set",
        "say": f"Done: you left the United States on {_words(left)} for {country} and {came}.",
    }


KEEP_TRAVEL = register(
    ToolSpec(
        "keep_travel",
        "Keep one trip the missionary took, as they say it: the day they left the United "
        "States (left, YYYY-MM-DD), the day they came back (back; leave it out while they're "
        "still away), and the country. One call a trip; a trip said again by its day left "
        "replaces it, and forget drops it. The days abroad on their tax packet are counted "
        "from these. The missionary tells Tim these, or whoever looks over every month does, "
        "naming them in person. It moves no money.",
        _schema(
            {
                "left": {"type": "string", "description": "The day they left, YYYY-MM-DD."},
                "back": {"type": "string", "description": "The day they came back."},
                "country": {"type": "string", "description": "Where they were, like Nepal."},
                "forget": {"type": "boolean", "description": "Drop the trip that left that day."},
                "person": {"type": "string", "description": "The missionary's name."},
            },
            ["left"],
        ),
        _keep_travel,
        annotations=_ACTS,
        short=(
            "Keep one trip as the missionary says it: left (the day they left the United States, "
            "YYYY-MM-DD), back (leave it out while they're still away), and country. One call a "
            "trip; the same day left again replaces it, and forget drops it. The days abroad on "
            "their tax packet are counted from these. person: the missionary, when their reviewer"
            " says it."
        ),
    ),
    _offered,
)
"""A missionary's trips (c38): the box's door, the person and their reviewer."""


# ---- who reads the packet, and what it shows of these -----------------------------------


def whose(t: Tools, args: dict) -> tuple[str, dict | None]:
    """Whose tax facts, trips, and packet these are: the person's own; for
    whoever looks over every month, the missionary they name (the owner,
    2026-11-15: the president does the missionary's books, so decision 2's
    privacy waits for a missionary with a tax picture of their own); or a
    missionary who shared their packet with the person asking. The unit, or
    a reply that stops the call."""
    said = " ".join(str(args.get("person") or "").split())
    me = t.viewer.person if t.viewer is not None else ""
    if said and unit_of(said) != me:
        unit = unit_of(said)
        if unit not in filers(t.viewer.policy):
            return "", {
                "status": "needs",
                "say": f"{said} has no tax packet at your ministry. Say the missionary's name.",
            }
        if _sees_every_month(t) or me in facts_of.shared_with(_said(t, facts_of.FACT_EVENT), unit):
            return unit, None
        return "", {
            "status": "refused",
            "say": "A missionary's tax packet is theirs and their ministry's reviewer's. They "
            "can share it with you by telling Tim.",
        }
    if not _files(t):
        return "", {"status": "needs", "say": "Say whose: the missionary's name."}
    return me, None


reader = whose  # the tax packet's reader (catalog's _tax_packet)


def said_back(t: Tools, unit: str, year: str) -> dict:
    """What the person told Tim for ``year``, for their packet."""
    f = facts_of.facts(_said(t, facts_of.FACT_EVENT), unit, year)
    out: dict = {"filing_status": facts_of.STATUS_WORDS.get(f.status, "")}
    if f.spouse_wages is not None:
        out["spouse"] = {"wages": money(f.spouse_wages), "withheld": money(f.spouse_withheld or 0)}
    if f.last_year_tax is not None:
        out["last_year_tax"] = money(f.last_year_tax)
    out["estimates_paid"] = [{"date": d, "dollars": money(c)} for d, c in f.estimates_paid]
    out["support_elsewhere"] = f.support_elsewhere
    out["trips"] = [
        {"left": tr.left, "back": tr.back, "country": tr.country}
        for tr in facts_of.trips(_said(t, facts_of.TRAVEL_EVENT), unit)
    ]
    return out
