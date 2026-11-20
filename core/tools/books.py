"""The ministry's books and statements: the books for any span, double
entry, the board pack, the CPA export, and the donor letters. Moved out of
core/tools/catalog.py.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.expenses import balances, budget, statements
from ..agents.expenses.categories import canonical
from ..agents.expenses.records import LANDED_EVENT
from ..agents.expenses.statements import money
from .months import _add, _month_report, _spent_by_kind
from .reads import _events, _name_of, _sees_every_month
from .spec import ToolSpec, _schema

if TYPE_CHECKING:
    from .catalog import Tools


def _cents_where(p: dict, role: str) -> int:
    return sum(
        int(Decimal(b["dollars"]) * 100)
        for b in p["bank"]
        if b["role"] == role and b["dollars"] is not None
    )


def _books(t: Tools, args: dict, keep: dict | None = None) -> dict:
    """The books for any span, as of any day (board item t-books; the owner,
    2026-10-29: month end, quarter and year, for every role, year to date
    too): what came in and what was spent, by kind, each month of it, what
    is owed back and paid back, what still waits, cash in hand at its end,
    and, in a year with a budget, each budget as of the span's end. Every
    figure is the month report's own, added up by code, so a person sees
    only the people their role lets them see. Read only."""
    from ..agents.expenses import periods

    today = t._today()
    try:
        span = periods.read(str(args.get("period") or ""), today)
    except periods.NotAPeriod:
        return {"status": "needs", "say": f"Say the span as {periods.SAY_PERIOD}."}
    who = str(args.get("person") or "").strip()
    year = str(span.end.year)
    budgeted = budget.latest((e for _i, e in _events(t, budget.BUDGET_EVENT)), year)
    # A budget counts from January 1, so its months are read even before the span.
    reach = periods.year_to_date(span.end.year, span.end) if budgeted else span
    first = span.start.strftime("%Y-%m")
    came: dict[str, int] = {}
    spent: dict[str, int] = {}
    year_spent: dict[str, int] = {}
    months, people, cash_at_end, named = [], set(), {}, set()
    owed_back = paid_back = waiting = 0
    fund: dict[str, int] = {}  # what each person drew less their own money in
    for month in reach.months(today):
        m_in = m_out = m_wait = 0
        for p in _month_report(t, {"month": month, "person": who})["people"]:
            named.add(p["person"])
            kinds = _spent_by_kind(p)
            _add(year_spent, kinds)
            if month < first:
                continue
            ins = [b for b in p["bank"] if b["role"] == statements.CAME_IN and b["dollars"]]
            for b in ins:
                kind = b["category"] or "Uncategorized"
                came[kind] = came.get(kind, 0) + int(Decimal(b["dollars"]) * 100)
            _add(spent, kinds)
            m_in += sum(int(Decimal(b["dollars"]) * 100) for b in ins)
            m_out += sum(kinds.values())
            m_wait += p["waiting_on_a_rate"] + p["bank_waiting"]
            owed_back += int(Decimal(p["total"]) * 100)
            paid_back += _cents_where(p, statements.PAID_BACK)
            # Net of their own money in (c31): their fund pays for what they drew.
            fund[p["person"]] = fund.get(p["person"], 0) + _cents_where(p, statements.REPAY)
            fund[p["person"]] -= _cents_where(p, statements.THEIRS_IN)
            if p["lines"] or p["bank"] or p["drives"]:
                people.add(p["person"])
            if p.get("cash_on_hand"):
                cash_at_end[p["person"]] = p["cash_on_hand"]
            elif p["person"] in cash_at_end:
                del cash_at_end[p["person"]]
        if month >= first:
            months.append(
                {"month": month, "came_in": money(m_in), "spent": money(m_out), "waiting": m_wait}
            )
            waiting += m_wait
    total_in, total_out = sum(came.values()), sum(spent.values())
    says = (
        f"{span.words}: {budget.dollars(total_in)} came in and "
        f"{budget.dollars(total_out)} was spent"
    )
    says += f"; {waiting} still wait on something." if waiting else "."
    report: dict = {
        "period": span.words,
        "from": span.start.isoformat(),
        "through": min(span.end, today).isoformat(),
        "for": "the whole ministry"
        if not who and _sees_every_month(t)
        else sorted(people) or (who or "you"),
        "says": says,
        "came_in": money(total_in),
        "came_in_by_kind": {k: money(v) for k, v in sorted(came.items())},
        "spent": money(total_out),
        "spent_by_kind": {k: money(v) for k, v in sorted(spent.items())},
        "came_in_less_spent": money(total_in - total_out),
        "owed_back_to_people": money(owed_back),
        "paid_back_to_people": money(paid_back),
        "owed_to_the_ministry": money(sum(c for c in fund.values() if c > 0)),
        **(
            {"held_for_people": {who: money(-c) for who, c in sorted(fund.items()) if c < 0}}
            if any(c < 0 for c in fund.values())
            else {}
        ),
        "still_waiting": waiting,
        "months": months,
        **({"cash_in_hand_at_end": cash_at_end} if cash_at_end else {}),
    }
    bank = _in_the_bank(t, named, min(span.end, today).isoformat())
    if bank:
        report["in_the_bank"] = bank if len(bank) > 1 else bank[0]
        for b in bank:
            report["says"] += f" {budget.dollars(int(Decimal(b['balance']) * 100))} in the bank" + (
                "."
                if b["agrees"]
                else "; the bank and the books disagree (in_the_bank says where)."
            )
    if budgeted:
        report["budget"] = budget.standing(
            budgeted, year_spent, year=year, today=min(span.end, today)
        )
    report["double_entry"] = _double_entry(
        t, who, first, min(span.end, today).isoformat(), named, keep
    )
    return report


def _double_entry(
    t: Tools, who: str, first: str, through: str, named: set[str], keep: dict | None = None
) -> dict:
    """The span's books in double entry (chunk c28, journal.py): every row
    the month report gives, from the books' first month through the span's
    end, as balanced entries; the span's own entries checked, and each
    account's balance at its end. The bank's account ties to the balance
    the bank itself gave, when it gave one."""
    from ..agents.expenses import journal

    log = [
        {"event_type": e["event_type"], "payload": e}
        for _i, e in _events(t, statements.LINE_EVENT, balances.BALANCE_EVENT)
    ]
    standing = [s for s in balances.standing(log, through) if s.person in named]
    starts = [str(e["payload"].get("day", ""))[:7] for e in log if e["payload"].get("day")]
    starts += [s.opening[0][:7] for s in standing]
    for _i, e in _events(t, LANDED_EVENT, statements.SCHOOL_EVENT):  # receipts before any bank line
        starts.append(str(e.get("day") or e.get("month") or "")[:7] or first)
    for r in t._rows("SELECT MIN(month) AS m FROM expense_report WHERE tenant = ?", (t.tenant,)):
        starts.append(str(r["m"] or first))
    month = min([first, *starts])
    everything: list[journal.Entry] = [
        journal.opening(s.person, s.opening[0], s.opening[1])
        for s in standing
        if s.opening[0] <= through
    ]
    waiting = 0
    while month <= through[:7]:
        made, w = journal.month_entries(_month_report(t, {"month": month, "person": who})["people"])
        everything += [e for e in made if e.day <= through]
        waiting += w if month >= first else 0
        month = _next_month(month)
    span = [e for e in everything if e.day[:7] >= first]
    found, at_end = journal.check(span), journal.totals(everything)
    if keep is not None:  # the board pack reads the same journal (c29)
        keep.update(span=journal.totals(span), at_end=at_end, entries=everything)
    out: dict = {
        "balanced": found.balanced and journal.check(everything).balanced,
        "entries": found.entries,
        "debits": money(found.debits),
        "credits": money(found.credits),
        "accounts_at_end": {a: money(_natural(a, c)) for a, c in at_end.items() if c},
        "income_by_kind": {
            k: money(v)
            for k, v in sorted(journal.by_kind(journal.totals(span), journal.INCOME).items())
        },
        "spending_by_kind": {
            k: money(v)
            for k, v in sorted(journal.by_kind(journal.totals(span), journal.EXPENSES).items())
        },
        **({"not_in_the_books_yet": waiting} if waiting else {}),
    }
    if standing:
        out["ties_to_the_bank"] = all(
            at_end.get(f"{journal.BANK}:{s.person}", 0) == s.books for s in standing
        )
    if found.unbalanced:
        out["unbalanced"] = [{"date": e.day, "words": e.words} for e in found.unbalanced]
    return out


def _natural(account: str, cents: int) -> int:
    """An account's balance the way a statement reads it: assets and
    spending as debits, the rest as credits."""
    return cents if account.startswith(("Assets", "Expenses")) else -cents


def _next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def _board_pack(t: Tools, args: dict) -> dict:
    """The ministry's statements for any span, from its own books (chunk
    c29, board_pack.py): the statement of activities, the statement of
    financial position at the span's end, the program and management split
    when the ministry set one, each month, and the budget. Read only; every
    figure is the books' own, so the two always agree."""
    from ..agents.expenses import board_pack as bp

    keep: dict = {}
    books = _books(t, args, keep)
    if "double_entry" not in books:
        return books
    acts, pos = bp.activities(keep["span"]), bp.position(keep["at_end"])
    out: dict = {
        "period": books["period"],
        "from": books["from"],
        "through": books["through"],
        "for": books["for"],
        "activities": {
            "came_in_by_kind": {k: money(v) for k, v in sorted(acts.income.items())},
            "came_in": money(acts.total_income),
            "spent_by_kind": {k: money(v) for k, v in sorted(acts.expenses.items())},
            "spent": money(acts.total_expenses),
            "change_in_net_assets": money(acts.change),
        },
        "position": {
            "as_of": books["through"],
            "in_the_bank": {k: money(v) for k, v in pos.in_the_bank.items()},
            "cash_in_hand": {k: money(v) for k, v in pos.cash_in_hand.items()},
            **(
                {
                    "with_payment_services": {
                        k: money(v) for k, v in pos.with_payment_services.items()
                    }
                }
                if pos.with_payment_services
                else {}
            ),
            "held_by_people": {k: money(v) for k, v in pos.held_by_people.items()},
            "owed_to_people": {k: money(v) for k, v in pos.owed_to_people.items()},
            "total_assets": money(pos.total_assets),
            "total_liabilities": money(pos.total_liabilities),
            "net_assets": money(pos.net_assets),
        },
        "months": books["months"],
        "matches_the_books": money(acts.total_income) == books["came_in"]
        and money(acts.total_expenses) == books["spent"],
        "balanced": books["double_entry"]["balanced"] and pos.ties,
    }
    if "ties_to_the_bank" in books["double_entry"]:
        out["ties_to_the_bank"] = books["double_entry"]["ties_to_the_bank"]
    share = bp.latest_share(e for _i, e in _events(t, bp.SPLIT_EVENT))
    if share is None:
        out["functional"] = {
            "say": "The ministry hasn't said what share of its spending is program; "
            "whoever looks over every month can (set_program_share)."
        }
    else:
        f = bp.functional(acts.expenses, share)
        out["functional"] = {
            "program_share": share,
            "program": money(f.program),
            "management": money(f.management),
            "by_kind": {
                k: {"program": money(p), "management": money(m)} for k, (p, m) in f.by_kind.items()
            },
        }
    if books.get("budget"):
        out["budget"] = books["budget"]
    out["says"] = (
        f"{books['period']}: {budget.dollars(acts.total_income)} came in, "
        f"{budget.dollars(acts.total_expenses)} was spent, and net assets were "
        f"{budget.dollars(pos.net_assets)} on {statements.day_words(books['through'])}."
    )
    if not out["balanced"] or not out["matches_the_books"]:
        out["says"] += " The statements don't tie to the books; say so and stop."
    return out


def _cpa_export(t: Tools, args: dict) -> dict:
    """A year's books for an accountant (chunk c30, export.py): the journal
    as Beancount-format text from what was brought forward, the trial
    balance as CSV, and the numbers the 990 and the 1099s start from; read
    back, it must balance and match the books; where the ministry paid pay,
    the 1099 total as pay only and each fund given toward (c34 PR 3). Read
    only."""
    from ..agents.expenses import church_pay, export
    from ..engine.config import load_tenant

    if not _sees_every_month(t):
        return {
            "status": "refused",
            "say": "The whole year's books go to whoever looks over every month.",
        }
    year = str(args.get("year") or t._today().year).strip()
    if not (len(year) == 4 and year.isascii() and year.isdigit()):
        return {"status": "needs", "say": "Say which year, as four digits, like 2026."}
    keep: dict = {}
    books = _books(t, {"period": year}, keep)
    if "entries" not in keep:
        return {"status": "needs", "say": "There are no books for that year yet."}
    first = f"{year}-01"
    before = [e for e in keep["entries"] if e.day[:7] < first]
    span = [e for e in keep["entries"] if e.day[:7] >= first]
    carried = export.brought_forward(before, f"{year}-01-01")
    entries = ([carried] if carried else []) + span
    cfg = load_tenant(t.tenant)
    listed = list(cfg.expenses.category_accounts)
    pay = str(args.get("pay_kind") or "").strip()
    pay = canonical(pay, listed) or "" if pay else ("Pay" if "Pay" in listed else "")
    name = cfg.identity.legal_name or t.tenant
    text = export.beancount(
        entries,
        title=f"{name}, books for {year}",
        notes=[f"From {books['from']} through {books['through']}."],
    )
    back = export.read_back(text)
    wrote = {export.account_name(a): c for a, c in journal_totals(entries).items() if c}
    year_totals = journal_totals(span)
    rows = [r for mine in _year_bank(t, year).values() for r in mine]
    numbers = export.filings(
        year_totals, keep["at_end"], pay, church_pay.by_person(rows, year), year
    )
    funds = church_pay.funds(rows, year, lambda who: _name_of(t, who))
    matches = (
        numbers["gross_receipts"] == books["came_in"]
        and numbers["total_expenses"] == books["spent"]
    )
    out = {
        "year": year,
        "round_trips": back.unbalanced == 0
        and {k: v for k, v in back.totals.items() if v} == wrote,
        "balanced": books["double_entry"]["balanced"],
        "matches_the_books": matches,
        "transactions": back.transactions,
        "filings": numbers,
        **({"funds": funds} if funds else {}),
        "beancount": text,
        "trial_balance_csv": export.trial_balance(journal_totals(entries)),
    }
    out["says"] = (
        f"{year}'s books for an accountant: {back.transactions} entries, "
        f"${Decimal(numbers['gross_receipts']):,.2f} gross receipts "
        f"({numbers['990'].split(' (')[0]}), ${Decimal(numbers['total_expenses']):,.2f} spent."
    )
    out["says"] += export.pay_words(numbers, lambda who: _name_of(t, who))
    if not (out["round_trips"] and out["balanced"] and matches):
        out["says"] += " The export doesn't tie to the books; say so and stop."
    return out


def journal_totals(entries: list) -> dict[str, int]:
    from ..agents.expenses import journal

    return journal.totals(entries)


def _year_of(t: Tools, args: dict) -> str | None:
    """The year a person asked about (this year when left out), or None
    when it isn't a year that has begun."""
    year = str(args.get("year") or t._today().year).strip()
    return year if re.fullmatch(r"\d{4}", year) and int(year) <= t._today().year else None


def _year_bank(t: Tools, year: str) -> dict[str, list[dict]]:
    """Each person's bank rows for a year, month by month through this one,
    read through the month report (shared by the donor letters and the tax
    packet, c32 and c33)."""
    out: dict[str, list[dict]] = {}
    for n in range(1, 13):
        month = f"{year}-{n:02d}"
        if month > t._today().strftime("%Y-%m"):
            break
        for p in _month_report(t, {"month": month})["people"]:
            out.setdefault(p["person"], []).extend(p["bank"])
    return out


def _donor_letters(t: Tools, args: dict) -> dict:
    """Each donor's gifts for a year and the letters they are owed (c32,
    donors.py): an acknowledgment of each gift of $250 or more, a year-end
    giving statement, and a thank-you for a grant from a donor-advised fund.
    Read only; the page and the PDF come from ``engine donor-letters``."""
    from ..agents.expenses import donors
    from ..engine.config import load_tenant

    today = t._today()
    year = _year_of(t, args)
    if year is None:
        return {"status": "needs", "say": "Say the year, like 2026."}
    rows = [r for mine in _year_bank(t, year).values() for r in mine]
    said = [e for _i, e in _events(t, donors.DETAIL_EVENT)]
    cfg = load_tenant(t.tenant)
    head = donors.letterhead(said, cfg.identity.legal_name or t.tenant)
    people, funds = donors.by_donor(donors.gifts_of(rows))
    out_people = []
    for d in people:
        letters = [
            f"an acknowledgment of the {statements.day_words(g.day)} gift of {donors.usd(g.cents)}"
            for g in d.acknowledged
        ]
        address = donors.address_of(said, d.name)
        out_people.append(
            {
                "name": d.name,
                "total": money(d.total),
                "gifts": [{"date": g.day, "dollars": money(g.cents)} for g in d.gifts],
                "letters": [*letters, "a year-end giving statement"],
                **({"address": address} if address else {}),
            }
        )
    unnamed = sum(
        1
        for r in rows
        if r.get("role") == statements.CAME_IN and r.get("dollars") and not donors.donor_of(r)
    )
    total = sum(d.total for d in people)
    says = (
        f"{year}: {len(people)} donor{'' if len(people) == 1 else 's'} gave "
        f"${Decimal(total) / 100:,.2f}"
        + (
            f", and {len(funds)} grant{'' if len(funds) == 1 else 's'} came from donor-advised "
            "funds"
            if funds
            else ""
        )
        + "."
    )
    if unnamed:
        says += f" {unnamed} gift{'' if unnamed == 1 else 's'} still name no donor: ask who gave."
    if head.missing():
        says += " Before the letters are made, Tim needs " + ", ".join(head.missing()) + "."
    return {
        "year": year,
        "as_of": today.isoformat(),
        "says": says,
        "donors": out_people,
        "fund_grants": [
            {
                "from": g.donor,
                "date": g.day,
                "dollars": money(g.cents),
                "letters": ["a thank-you"],
                **({"address": a} if (a := donors.address_of(said, g.donor)) else {}),
            }
            for g in funds
        ],
        "unnamed": unnamed,
        "needs": head.missing(),
        "letterhead": {k: v for k, v in vars(head).items() if v},
    }


def _letter_details(t: Tools, args: dict) -> dict:
    """What the donor letters need that the books don't hold, as said (c32):
    the ministry's EIN and mailing address, who signs and their title, a line
    on what gifts did, and a donor's mailing address. Newest wins."""
    from ..agents.expenses import donors
    from ..engine.runner import door_run as run

    head = {k: " ".join(str(args.get(k) or "").split()) for k in donors._HEAD}
    head["address"] = str(args.get("address") or "").strip()
    head = {k: v for k, v in head.items() if v}
    if "ein" in head and not donors.EIN.match(head["ein"]):
        return {"status": "needs", "say": "An EIN is nine digits, like 12-3456789. Ask again."}
    if "ein" in head and "-" not in head["ein"]:
        head["ein"] = f"{head['ein'][:2]}-{head['ein'][2:]}"
    donor = " ".join(str(args.get("donor") or "").split())
    where = str(args.get("donor_address") or "").strip()
    if bool(donor) != bool(where):
        return {
            "status": "needs",
            "say": "Say the donor's name and their mailing address together.",
        }
    said = [head] if head else []
    if donor:
        said.append({"donor": donor, "address": where})
    if not said:
        return {
            "status": "needs",
            "say": "Say what the letters need: the ministry's EIN, its "
            "mailing address, who signs and their title, or a donor's mailing address.",
        }
    by = t.viewer.person if t.viewer is not None else ""
    for params in said:
        done = run(
            t.tenant,
            "expenses",
            "letter_detail",
            params={**params, "by": by},
            ledger_dir=Path(t.receipts.ledger_root).parent,
            tenants_root=t.receipts.tenants_root,
        )
        if done.status == "error":
            return {"status": "busy", "say": "Tim couldn't keep that just now. Try again soon."}
    return {"status": "set", "say": "Done: the donor letters will say it from now on."}


def _in_the_bank(t: Tools, named: set[str], through: str) -> list[dict]:
    """The ministry's account as of ``through``: the bank's own balance
    against the books (balances.py), for the people this person may see."""
    log = [
        {"event_type": e["event_type"], "payload": e}
        for _i, e in _events(t, statements.LINE_EVENT, balances.BALANCE_EVENT)
    ]
    out = []
    for s in balances.standing(log, through):
        if s.person not in named:
            continue
        one: dict = {
            "balance": money(s.books),
            "bank_said": money(s.bank),
            "bank_said_on": s.bank_on,
            "opening": money(s.opening[1]),
            "opening_on": s.opening[0],
            "agrees": not s.gaps,
        }
        if s.gaps:
            one["gaps"] = [
                {"after": g.after, "on": g.on, "bank_less_books": money(g.cents)} for g in s.gaps
            ]
            one["say"] = balances.words(s)
        out.append(one | ({"person": s.person} if len(named) > 1 else {}))
    return out


CPA_EXPORT = ToolSpec(
    "cpa_export",
    "A year's books for an accountant, when the person asks for them: the whole journal "
    "as plain text in the Beancount format (beancount), the trial balance as CSV "
    "(trial_balance_csv), and the numbers the 990 and the 1099s start from (filings). Save "
    "the two texts as files for them when they want files; read them the says line and the "
    "filings. pay_kind names the ministry's kind for pay when it isn't called Pay. Only "
    "someone who looks over every month may ask. Read only.",
    _schema(
        {
            "year": {"type": "string", "description": "The year, as four digits."},
            "pay_kind": {"type": "string", "description": "The ministry's kind for pay."},
        }
    ),
    _cpa_export,
    short=(
        "A year's books for an accountant, when the person asks: the journal in the Beancount "
        "format (beancount), the trial balance as CSV (trial_balance_csv), and the numbers the "
        "990 and the 1099s start from (filings). Read them the says line and the filings. "
        "pay_kind names the ministry's kind for pay when it isn't called Pay. Read only."
    ),
)
"""The books for an accountant (c30): on the box's door, for whoever looks over every month."""


DONOR_LETTERS = ToolSpec(
    "donor_letters",
    "The ministry's donors for a year (leave year out for this year): who gave, each gift's "
    "day and dollars, each donor's total, and the letters each is owed: a written "
    "acknowledgment of each gift of $250 or more and a year-end giving statement, or for a "
    "grant from a donor-advised fund a thank-you only. A gift is money in on the ministry's "
    "account whose donor was named (explain_bank_line's gift_from); unnamed counts the ones "
    "still waiting on who gave. needs lists what the letters still need, said once with "
    "letter_details. Start with the says line; quote the dollars as given.",
    _schema({"year": {"type": "string", "description": "Like 2026."}}),
    _donor_letters,
    short=(
        "The ministry's donors for a year (year left out for this year): who gave, each gift, "
        "each donor's total, and the letters each is owed (an acknowledgment of each gift of $250"
        " or more and a year-end giving statement; for a donor-advised fund's grant, a thank-you "
        "only). unnamed: gifts still waiting on who gave (explain_bank_line's gift_from). needs: "
        "what the letters still need, said once with letter_details. Start with the says line."
    ),
)
"""Donors and their letters (c32): any door, for whoever looks over every month."""


LETTER_DETAILS = ToolSpec(
    "letter_details",
    "Keep what the donor letters need that the books don't hold, in the person's own words: "
    "the ministry's EIN, its mailing address, who signs the letters and their title, a "
    "sentence on what gifts did this year (thanks), and a donor's mailing address (donor "
    "with donor_address). Pass only what they said; the newest word on each stands. Only "
    "someone who looks over every month may set them.",
    _schema(
        {
            "ein": {"type": "string", "description": "Nine digits, like 12-3456789."},
            "address": {"type": "string", "description": "The ministry's mailing address."},
            "signer": {"type": "string", "description": "Who signs the letters."},
            "title": {"type": "string", "description": "The signer's title, like Treasurer."},
            "thanks": {"type": "string", "description": "What gifts did this year, one sentence."},
            "donor": {"type": "string", "description": "A donor's name, with donor_address."},
            "donor_address": {"type": "string", "description": "That donor's mailing address."},
        }
    ),
    _letter_details,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Keep what the donor letters need that the books don't hold, in the person's own words: "
        "ein, address, signer and title, thanks (a sentence on what gifts did this year), and a "
        "donor's donor_address. Pass only what they said; the newest word on each stands."
    ),
)
"""The letters' details (c32): on the box's door, for whoever looks over every month."""
