"""The missionary's tax packet, its arithmetic (board chunk c33; the owner,
2026-11-12: one plain tax packet in January). Pure functions; the door's
``tax_packet`` reads the payers (``payers.py``) and lays these beside them.

- Self-employment tax: the Social Security and Medicare a self-employed
  person pays both halves of, 15.3% of 92.35% of their net earnings; the
  Social Security part (12.4%) only up to the year's wage base, the Medicare
  part (2.9%) on all of it, and none at all below $400 of earnings. Half of
  it comes off their income. The Foreign Earned Income Exclusion lowers
  income tax only, never this.
- The days abroad: counted from the travel dates the person said (c38 PR 3,
  ``core/tax/presence.py``), full days only, the best 12 months in a row
  against the physical presence test's 330. The bank's foreign spending is
  the cross-check: each day it shows spending abroad that falls on a day
  they said they were in the United States is named. With no travel dates,
  the bank's days are a draft that names its long gaps.
- The four estimate dates: the 15th of April, June, September, and the next
  January, moved past a weekend or a federal holiday that falls on it.

The arithmetic lives in ``core/tax/`` (board chunk c36) and the year's
figures in ``core/tax/tables/<year>.toml``, each with its IRS source;
``self_employment`` is re-exported here for the callers that know it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from ...tax import estimates, exclusion, form1040, mileage, presence
from ...tax.se import SelfEmployment as SelfEmployment
from ...tax.se import figures_from as figures_from
from ...tax.se import self_employment as self_employment
from . import payers, statements
from .statements import money

PRESENCE_DAYS = 330  # full days abroad in any 12 months in a row
LONG_GAP = 14  # days between spending abroad worth asking about


# ---- the four estimate dates ------------------------------------------------------------


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7 + 7 * (n - 1))


def _kept_on(day: date) -> date:
    """The weekday a holiday on ``day`` is kept on."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _holidays(year: int) -> set[date]:
    """The federal holidays near an estimate date: Martin Luther King Jr. Day
    (January), and Emancipation Day (April 16), which moves the April date
    when kept beside it."""
    return {_nth_monday(year, 1, 3), _kept_on(date(year, 4, 16)), _kept_on(date(year, 1, 1))}


def _due(day: date) -> date:
    while day.weekday() >= 5 or day in _holidays(day.year):
        day += timedelta(days=1)
    return day


def estimate_dates(year: str) -> list[date]:
    """When the four quarterly estimates for ``year`` are due."""
    y = int(year)
    return [_due(d) for d in (date(y, 4, 15), date(y, 6, 15), date(y, 9, 15), date(y + 1, 1, 15))]


# ---- the days abroad --------------------------------------------------------------------

_ABROAD = re.compile(r"\b([A-Z]{2}) (?:[A-Z][a-z]+ )+[\d,]+\.\d{2} X\b")
"""A card line's foreign amount, as a U.S. bank prints it: "NP Rupee 20000.00 X"."""
_GAVE = re.compile(r"^([A-Z]{3}) ")


def foreign_days(rows: Iterable[dict]) -> dict[str, str]:
    """Each day the bank shows money spent in another currency, with the
    country it names (its currency's code when no country is printed)."""
    out: dict[str, str] = {}
    for r in rows:
        words, gave = str(r.get("words") or ""), str(r.get("gave") or "")
        found = _ABROAD.search(words)
        where = found[1] if found else ""
        if not where and (g := _GAVE.match(gave)) and g[1] != "USD":
            where = g[1]
        if where:
            out.setdefault(str(r["date"]), where)
    return dict(sorted(out.items()))


def days_abroad(rows: Iterable[dict], year: str) -> dict:
    """The year's days abroad, drafted from the bank: how many days show
    spending abroad, the first and last, the countries, and each gap of more
    than two weeks between them, for the person to say where they were."""
    days = {d: c for d, c in foreign_days(rows).items() if d.startswith(year)}
    if not days:
        return {
            "days_with_spending_abroad": 0,
            "needs": PRESENCE_DAYS,
            "says": f"The bank shows no spending abroad in {year}. If they lived abroad, ask for "
            "their travel dates: the exclusion needs 330 full days abroad in 12 months.",
        }
    seen = list(days)
    gaps = []
    for a, b in zip(seen, seen[1:], strict=False):
        n = (date.fromisoformat(b) - date.fromisoformat(a)).days
        if n > LONG_GAP:
            gaps.append({"from": a, "to": b, "days": n})
    says = (
        f"The bank shows spending abroad on {len(days)} days in {year}, "
        f"{_short(seen[0])} through {_short(seen[-1])}. The exclusion needs 330 full days "
        "abroad in 12 months in a row, and only they know their travel dates"
    )
    says += (
        f": ask where they were in the {len(gaps)} stretch{'' if len(gaps) == 1 else 'es'} "
        "of more than two weeks with none."
        if gaps
        else ": ask for them."
    )
    return {
        "days_with_spending_abroad": len(days),
        "first": seen[0],
        "last": seen[-1],
        "countries": sorted(set(days.values())),
        "gaps": gaps,
        "needs": PRESENCE_DAYS,
        "says": says,
    }


def at_home(days: Iterable[str], trips: Iterable[dict]) -> list[str]:
    """The days among ``days`` that fall on no trip they said, from the day
    they left through the day they came back (or on, while still away):
    days they said they were in the United States."""
    spans = [(t["left"], t.get("back") or "9999-12-31") for t in trips]
    return [d for d in days if not any(a <= d <= b for a, b in spans)]


def from_travel(rows: Iterable[dict], year: str, trips: list[dict], today: date | None) -> dict:
    """The year's days abroad from the travel dates they said, the bank's
    foreign spending beside it, and each bank day abroad that falls on a day
    they said they were home."""
    out = days_abroad(rows, year)
    got = presence.best(
        [presence.Stay(t["left"], t.get("back") or "") for t in trips], year, today=today
    )
    if got.start is None or got.end is None:
        return out
    home = at_home([d for d in foreign_days(rows) if d.startswith(year)], trips)
    out = {
        "full_days": got.days,
        "from": got.start.isoformat(),
        "to": got.end.isoformat(),
        "qualifies": got.qualifies,
        **({"days_still_ahead": got.ahead} if got.ahead else {}),
        **{k: v for k, v in out.items() if k != "says"},
        "bank_abroad_on_days_home": home,
    }
    says = (
        f"From their travel dates, {got.days} full days abroad in the 12 months "
        f"{_short(out['from'])}, {got.start.year} through {_short(out['to'])}, {got.end.year}: "
    )
    says += (
        f"the exclusion's {PRESENCE_DAYS} are met"
        if got.qualifies
        else f"{PRESENCE_DAYS - got.days} short of the {PRESENCE_DAYS} the exclusion needs"
    )
    says += (
        f", {got.ahead} of them still ahead, so only if they stay through {_short(out['to'])}."
        if got.ahead and got.qualifies
        else "."
    )
    if home:
        n = len(home)
        says += (
            f" The bank shows spending abroad on {n} day{'' if n == 1 else 's'} they said they "
            f"were in the United States ({', '.join(_short(d) for d in home[:5])}"
            f"{', and more' if n > 5 else ''}): ask, and fix the trip if it was said wrong."
        )
    out["says"] = says
    return out


def _short(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}"


# ---- the tax half of the tax_packet answer ----------------------------------------------


def taxes(
    year: str,
    out_payers: list[dict],
    rows: list[dict],
    says: list[str],
    told: dict | None = None,
    today: date | None = None,
    drives: Iterable[tuple[Decimal, Decimal, Decimal]] = (),
    receipts: tuple[Iterable[int], int] = ((), 0),
) -> dict:
    """The ``tax_packet`` answer's tax half (c33; moved from the door's
    catalog, c36; the roll-up, c39): the 1099 income less the costs it
    carried (Schedule C), self-employment tax on it, the days abroad (from
    the travel dates said, else drafted from the bank), the federal return
    worked out to a bracket, with the Foreign Earned Income Exclusion and
    without it when they qualify, and next year's four estimates. ``told``
    is what the person said (``tax_facts.said_back``). ``drives`` are their
    ministry drives paid back below the IRS rate (``drives.of_year``); the
    difference is a cost (c34 PR 6). ``receipts`` are the cents of their
    receipts paid from their own pay, and how many wait on a rate
    (``own_pay.of_year``, c34 gap C). Adds its lines to ``says``."""
    told = told or {}
    trips = told.get("trips") or []

    def cents(p: dict) -> int:
        return int(Decimal(p.get("form_says") or p["came_in_seen"]) * 100)

    income = sum(cents(p) for p in out_payers if p["form"] != payers.W2)  # wages never are
    no_form = sum(cents(p) for p in out_payers if not p["form"])  # gifts for their work (c38)
    w2 = [p for p in out_payers if p["form"] == payers.W2 and "form_says" in p]
    wages = sum(int(Decimal(p["form_says"]) * 100) for p in w2)
    withheld = sum(int(Decimal(p.get("withheld") or 0) * 100) for p in w2)
    costs = sum(
        abs(int(Decimal(r["dollars"]) * 100))
        for r in rows
        if r.get("role") == statements.FROM_PAY and r.get("dollars")
    )
    paid_receipts, receipts_waiting = receipts
    costs += sum(paid_receipts)
    unpaid_miles = mileage.below_irs(drives)
    costs += unpaid_miles
    net = income - costs
    out: dict = {
        "schedule_c": {
            "income": money(income),
            **({"with_no_form": money(no_form)} if no_form else {}),
            "costs": money(costs),
            **({"mileage_below_irs_rate": money(unpaid_miles)} if unpaid_miles else {}),
            "net": money(net),
        }
    }
    try:
        se = self_employment(max(net, 0), year, wages=wages)
    except KeyError:
        says.append(f"Tim doesn't hold {year}'s tax figures yet, so it can't work out the tax.")
        return out
    out["self_employment_tax"] = {
        "on": money(se.earnings),
        "tax": money(se.tax),
        "half_off_income": money(se.half),
        "figures_from": figures_from(year),
    }
    if costs - unpaid_miles:
        says.append(
            f"${Decimal(costs - unpaid_miles) / 100:,.2f} of ministry costs they paid from "
            "their own pay come off it."
        )
    if receipts_waiting:
        says.append(
            "1 receipt they paid from their own pay waits on its day's exchange rate; Tim "
            "counts it once the month runs."
            if receipts_waiting == 1
            else f"{receipts_waiting} receipts they paid from their own pay wait on their "
            "days' exchange rates; Tim counts them once the month runs."
        )
    if unpaid_miles:
        says.append(
            f"${Decimal(unpaid_miles) / 100:,.2f} of their ministry drives, the part the "
            "ministry paid back below the IRS mileage rate, comes off it as a car cost."
        )
    if se.tax:
        says.append(
            f"Self-employment tax on ${Decimal(net) / 100:,.2f} of 1099 income, net of costs, is "
            f"${Decimal(se.tax) / 100:,.2f}; the Foreign Earned Income Exclusion never lowers it."
        )
    out["days_abroad"] = from_travel(rows, year, trips, today) if trips else days_abroad(rows, year)
    says.append(out["days_abroad"]["says"])
    abroad = None
    if out["days_abroad"].get("qualifies"):
        stays = [presence.Stay(t["left"], t.get("back") or "") for t in trips]
        qualifying, full = presence.in_year(stays, presence.best(stays, year, today), year)
        whole = exclusion.year_days(int(year))
        abroad = exclusion.Abroad(
            income=exclusion.earned_abroad(income, full, whole),
            costs=exclusion.earned_abroad(costs, full, whole),
            qualifying_days=qualifying,
        )
    out["federal_tax"] = _federal(
        year, net, wages, withheld, told, abroad, bool(trips), today, says
    )
    return out


_STATUS = {"single": "single", "married filing jointly": "married_jointly"}


def _federal(
    year: str,
    net: int,
    wages: int,
    withheld: int,
    told: dict,
    abroad: exclusion.Abroad | None,
    traveled: bool,
    today: date | None,
    says: list[str],
) -> dict:
    """The roll-up for the packet (the arithmetic is ``core/tax``'s): without
    the exclusion always, with it beside when they qualify, never chosen for
    them (the plan's section 3.6, rule 6)."""
    status = _STATUS.get(told.get("filing_status") or "", "")
    if not status:
        says.append("Their filing status isn't said, so this is worked out as single: ask.")
    spouse = told.get("spouse") if status == "married_jointly" else None
    spouse_wages = int(Decimal(spouse["wages"]) * 100) if spouse else 0
    spouse_withheld = int(Decimal(spouse["withheld"]) * 100) if spouse else 0
    paid = sum(int(Decimal(e["dollars"]) * 100) for e in told.get("estimates_paid") or [])
    part = today is not None and today < date(int(year), 12, 31)

    def version(at: exclusion.Abroad | None) -> tuple[form1040.Return, dict]:
        r = form1040.roll_up(
            year,
            status or "single",
            schedule_c_net=max(net, 0),
            wages=wages,
            spouse_wages=spouse_wages,
            withheld=withheld + spouse_withheld,
            abroad=at,
        )
        ahead = estimates.plan(
            year,
            r.total_tax,
            withheld=r.withheld,
            last_year_tax=r.total_tax,
            last_year_agi=r.adjusted_gross_income,
        )
        v = {
            **({"excluded": money(r.exclusion.net)} if r.exclusion else {}),
            "adjusted_gross_income": money(r.adjusted_gross_income),
            "standard_deduction": money(r.standard_deduction),
            "business_deduction": money(r.business_deduction),
            "taxable_income": money(r.taxable_income),
            "income_tax": money(r.income_tax),
            "top_bracket": f"{r.bracket * 100:.0f}" if r.taxable_income else "",
            "self_employment_tax": money(r.self_employment.tax),
            "total_tax": money(r.total_tax),
            "withheld": money(r.withheld),
            "estimates_paid": money(paid),
            "balance": money(r.balance - paid),
            "estimates": [
                {"due": d.isoformat(), "dollars": money(c)}
                for d, c in zip(
                    estimate_dates(str(int(year) + 1)), estimates.four(ahead.required), strict=True
                )
            ],
        }
        return r, v

    without, plain = version(None)
    got: dict = {
        "filing_status": status or "single",
        **({"part_year_through": today.isoformat()} if part and today else {}),
        "without_exclusion": plain,
    }
    if part and today:
        says.append(
            f"Worked out on the year so far, through {_short(today.isoformat())}: a part-year "
            "figure; the full year will differ."
        )
    says.append(_tax_words(plain))
    if abroad is not None:
        _, taken = version(abroad)
        got["with_exclusion"] = taken
        says.append(
            f"With the Foreign Earned Income Exclusion, income tax is ${_d(taken['income_tax'])} "
            f"and total federal tax ${_d(taken['total_tax'])}; without it, "
            f"${_d(plain['income_tax'])} and ${_d(plain['total_tax'])}. The choice is theirs, "
            "with their preparer: taking it gives up the refundable part of the child tax "
            "credit and the earned income credit."
        )
    elif traveled:
        says.append(
            "Their travel dates fall short of the 330 full days, so this is worked out without "
            "the Foreign Earned Income Exclusion."
        )
    else:
        says.append(
            "With no travel dates said, it's worked out without the Foreign Earned Income "
            "Exclusion."
        )
    first = (got.get("with_exclusion") or plain)["estimates"][0]["dollars"]
    nxt = int(year) + 1
    says.append(
        f"{nxt}'s four estimated payments come to about ${_d(first)} each."
        if Decimal(first)
        else f"No estimated payments are needed for {nxt}: under $1,000 would be owed."
    )
    if paid:
        says.append(f"${Decimal(paid) / 100:,.2f} in estimates already paid toward {year}.")
    got["left_to_preparer"] = list(without.left_out)
    got["figures_from"] = without.figures_from
    return got


def _d(dollars: str) -> str:
    return f"{Decimal(dollars):,.2f}"


def _tax_words(v: dict) -> str:
    if not v["top_bracket"]:
        return (
            "No federal income tax: nothing is taxable after the standard deduction. Total "
            f"federal tax, all of it self-employment tax, is ${_d(v['total_tax'])}."
        )
    return (
        f"Federal income tax is ${_d(v['income_tax'])}, top bracket {v['top_bracket']} percent; "
        f"total federal tax with self-employment tax is ${_d(v['total_tax'])}."
    )


# ---- the page (engine tax-packet; drawn by core/tools/paper.py) -------------------------

_FORMS = {
    "1099-NEC": "Form 1099-NEC (nonemployee compensation)",
    "W-2": "Form W-2 (wage and tax statement)",
}


def _usd(dollars: str) -> str:
    return f"${Decimal(dollars):,.2f}"


def page(got: dict) -> list[tuple]:
    """The ``tax_packet`` answer as one page of blocks, in the formal
    register, every acronym spelled out: income from each payer, Schedule C,
    self-employment tax, the days abroad, and next year's estimates. A
    section whose figures Tim doesn't hold for the year is left out."""
    from .donors import long_day

    year = got["year"]
    blocks: list[tuple] = [
        ("title", f"{year} tax packet for {got['person']}"),
        (
            "small",
            f"Prepared {long_day(got['as_of'])} from the bank lines Tim sees. This is a draft "
            "for the person and their tax preparer; nothing on it has been filed.",
        ),
    ]
    rows = []
    for p in got["payers"]:
        named = f"{p['name']}, a {p['kind']}:"
        if "work_share" in p:  # some of each: only the part for their work is pay
            named = f"{p['name']}: {p['work_share']} of each payment, for their ministry work,"
        rows.append(
            (
                f"{named} seen arriving in the bank",
                _usd(p["came_in_seen"]),
                "form_says" not in p and p["form"] != payers.W2,
            )
        )
        if "form_says" in p:
            rows.append(
                (
                    f"Its {_FORMS.get(p['form'], 'Form ' + p['form'])} says",
                    _usd(p["form_says"]),
                    True,
                )
            )
        if "withheld" in p:
            rows.append(("Federal income tax withheld, its box 2", _usd(p["withheld"]), False))
        if "not_seen" in p:
            rows.append(("Paid to an account Tim does not see", _usd(p["not_seen"]), False))
    blocks.append(("table", "Income from each payer", rows))
    for u in got["unmatched"]:
        blocks.append(
            ("text", f"Ask who paid: {_usd(u['dollars'])} on {long_day(u['date'])} ({u['words']}).")
        )
    for a in got["asks"]:
        when = f"{_usd(a['dollars'])} on {long_day(a['date'])}" if "date" in a else a["payer"]
        blocks.append(("text", f"{when}: {a['ask']}"))
    if got["personal_gifts"]:
        total = sum(Decimal(g["dollars"]) for g in got["personal_gifts"])
        n = len({g["from"] for g in got["personal_gifts"]})
        blocks.append(
            (
                "small",
                f"Personal gifts are kept off this packet: {_usd(str(total))} from {n} "
                f"giver{'' if n == 1 else 's'}. A gift is not income.",
            )
        )
    c = got["schedule_c"]
    no_form = c.get("with_no_form")
    reported = Decimal(c["income"]) - Decimal(no_form or 0)
    blocks.append(
        (
            "table",
            "Schedule C, Profit or Loss From Business",
            [
                (f"Income reported on {_FORMS['1099-NEC']}", _usd(str(reported)), False),
                *(
                    [("Gifts for their ministry work, with no tax form", _usd(no_form), False)]
                    if no_form
                    else []
                ),
                ("Ministry costs paid from their own pay", _usd(c["costs"]), False),
                ("Net profit", _usd(c["net"]), True),
            ],
        )
    )
    se = got.get("self_employment_tax")
    if se is None:
        blocks.append(
            ("text", f"Tim does not hold the {year} tax figures yet, so the tax is not worked out.")
        )
        return blocks
    blocks.append(
        (
            "table",
            "Self-employment tax, Schedule SE (Social Security and Medicare)",
            [
                ("Net earnings, 92.35% of the net profit", _usd(se["on"]), False),
                ("Self-employment tax", _usd(se["tax"]), True),
                ("Half of it, deducted from income", _usd(se["half_off_income"]), False),
            ],
        )
    )
    blocks.append(
        (
            "text",
            "The Foreign Earned Income Exclusion lowers income tax only; it never lowers "
            "self-employment tax.",
        )
    )
    d = got["days_abroad"]
    if "full_days" in d:
        blocks += _travel_days(d)
    else:
        blocks += _bank_days(d)
    blocks += _return(got["federal_tax"], year)
    blocks.append(("small", f"Tax figures from: {se['figures_from']}."))
    return blocks


def _return(f: dict, year: str) -> list[tuple]:
    """The federal return worked out to a bracket (c39): without the
    exclusion, and with it beside when they qualify, then next year's
    estimates and what Tim left to the preparer."""
    from .donors import long_day

    out: list[tuple] = []
    if "part_year_through" in f:
        out.append(
            (
                "text",
                f"Worked out on {year} so far, through {long_day(f['part_year_through'])}. "
                "This is a part-year figure; the full year will differ.",
            )
        )
    taken = f.get("with_exclusion")
    status = "married filing jointly" if f["filing_status"] == "married_jointly" else "single"
    for title, v in (
        ("without the Foreign Earned Income Exclusion" if taken else "", f["without_exclusion"]),
        *((("with the Foreign Earned Income Exclusion", taken),) if taken else ()),
    ):
        rows = [
            *(
                [("Foreign earned income excluded, net (Form 2555)", _usd(v["excluded"]), False)]
                if "excluded" in v
                else []
            ),
            ("Adjusted gross income", _usd(v["adjusted_gross_income"]), False),
            (f"Standard deduction, {status}", _usd(v["standard_deduction"]), False),
            ("Qualified business income deduction", _usd(v["business_deduction"]), False),
            ("Taxable income", _usd(v["taxable_income"]), False),
            ("Federal income tax", _usd(v["income_tax"]), False),
            (
                "Top bracket",
                f"{v['top_bracket']} percent" if v["top_bracket"] else "none",
                False,
            ),
            ("Self-employment tax", _usd(v["self_employment_tax"]), False),
            ("Total federal tax", _usd(v["total_tax"]), True),
            ("Federal income tax withheld", _usd(v["withheld"]), False),
            *(
                [("Estimated payments already made", _usd(v["estimates_paid"]), False)]
                if Decimal(v["estimates_paid"])
                else []
            ),
            ("Left to pay (a refund when negative)", _usd(v["balance"]), True),
        ]
        out.append(
            ("table", f"Federal tax for {year}, Form 1040{', ' + title if title else ''}", rows)
        )
    if taken:
        out.append(
            (
                "text",
                "They qualify for the Foreign Earned Income Exclusion, so the tax is shown both "
                "ways. The choice is theirs, with their preparer. Taking it gives up the "
                "refundable part of the child tax credit and the earned income credit, and "
                "taking either of those later revokes the exclusion for 5 years.",
            )
        )
    for title, v in (
        ("" if not taken else ", without the exclusion", f["without_exclusion"]),
        *(((", with the exclusion", taken),) if taken else ()),
    ):
        est = [(f"Due {long_day(e['due'])}", _usd(e["dollars"]), False) for e in v["estimates"]]
        est.append(("Total", _usd(str(sum(Decimal(e["dollars"]) for e in v["estimates"]))), True))
        out.append(
            ("table", f"Estimated tax payments for {int(year) + 1}, Form 1040-ES{title}", est)
        )
    out.append(
        (
            "text",
            f"The estimates assume {int(year) + 1} looks like {year}: 90% of this year's total "
            "federal tax, less the same withholding, a quarter each. None are needed when under "
            "$1,000 would be owed.",
        )
    )
    out.append(
        (
            "table",
            "What Tim left to your preparer",
            [(item[0].upper() + item[1:], "", False) for item in f["left_to_preparer"]],
        )
    )
    return out


def _bank_days(d: dict) -> list[tuple]:
    """The days-abroad section drafted from the bank (no travel dates said)."""
    from .donors import long_day

    out: list[tuple] = []
    days = [("Days the bank shows spending abroad", str(d["days_with_spending_abroad"]), False)]
    if d["days_with_spending_abroad"]:
        days.append(("First and last", f"{long_day(d['first'])} to {long_day(d['last'])}", False))
    for g in d.get("gaps", []):
        days.append(
            (
                f"No spending abroad from {long_day(g['from'])} to {long_day(g['to'])}",
                f"{g['days']} days",
                False,
            )
        )
    days.append(
        ("Full days abroad the exclusion needs, in 12 months in a row", str(d["needs"]), True)
    )
    out.append(("table", "Days abroad, a draft for the Foreign Earned Income Exclusion", days))
    out.append(
        (
            "text",
            "A day with spending in another currency counts as a day abroad. Only the person "
            "knows their travel dates, so each long stretch above needs their word before the "
            "exclusion is claimed.",
        )
    )
    return out


def _travel_days(d: dict) -> list[tuple]:
    """The days-abroad section counted from the travel dates said (c38 PR 3),
    the bank's days beside it."""
    from .donors import long_day

    rows = [
        (
            f"Full days abroad from {long_day(d['from'])} to {long_day(d['to'])}, from their "
            "travel dates",
            str(d["full_days"]),
            True,
        ),
        ("Full days abroad the exclusion needs, in 12 months in a row", str(d["needs"]), False),
        ("Days the bank shows spending abroad", str(d["days_with_spending_abroad"]), False),
    ]
    rows += [
        (f"Spending abroad on {long_day(day)}, a day they said they were home", "ask", False)
        for day in d.get("bank_abroad_on_days_home", [])
    ]
    note = (
        "Counted from the travel dates the person gave: full days only, so the day they left "
        "and the day they came back are not counted."
    )
    if d.get("days_still_ahead"):
        note += (
            f" {d['days_still_ahead']} of the days are still ahead and count only if they stay "
            f"abroad through {long_day(d['to'])}."
        )
    return [
        ("table", "Days abroad, for the Foreign Earned Income Exclusion", rows),
        ("text", note),
    ]
