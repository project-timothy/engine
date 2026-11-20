"""A missionary's tax packet and the payers who send them a tax form, and
the church's pay for its people. Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.expenses.records import DRIVE_EVENT, PURPOSE_EVENT
from .books import _year_bank, _year_of
from .reads import _events, _log, _sees_every_month
from .rules import _decides_rules
from .spec import ToolSpec, _schema
from .viewer import display_name, unit_of

if TYPE_CHECKING:
    from .catalog import Tools

SPLIT_WORDS: dict[str, str] = tomllib.loads(
    Path(__file__).with_name("payer_split.toml").read_text(encoding="utf-8")
)["words"]
"""What Tim says of a payer who sends some of each (t-payer-split)."""


def _has_a_packet(t: Tools) -> bool:
    """Whether the person has a tax packet to see (c33): a missionary, who
    files their own months, or whoever looks over every month."""
    from .viewer import filers

    return t.viewer.person in filers(t.viewer.policy) or _sees_every_month(t)


def _packet_person(t: Tools, args: dict) -> tuple[str, dict | None]:
    """Whose payers and packet these are (c33): the person's own, or for
    whoever looks over every month, the missionary they name (the owner,
    2026-11-13). The unit, or a reply that stops the call."""
    from .viewer import filers

    said = " ".join(str(args.get("person") or "").split())
    me = t.viewer.person if t.viewer is not None else ""
    if said and unit_of(said) != me:
        if not _sees_every_month(t):
            return "", {
                "status": "refused",
                "say": "A missionary's packet is theirs, and whoever looks over every month "
                "at your ministry.",
            }
        return unit_of(said), None
    if not me or me not in filers(t.viewer.policy):
        return "", {"status": "needs", "say": "Say whose payers these are: the missionary's name."}
    return me, None


def _keep_payers(t: Tools, args: dict) -> dict:
    """Keep one of a missionary's payers, what its tax form reports for a
    year (a W-2's wages with its tax withheld), or whether a gift comes for
    their ministry work (c33, c38; payers.py), in the person's own words.
    Newest wins."""
    from ..agents.expenses import payers
    from ..engine.runner import door_run as run

    unit, stop = _packet_person(t, args)
    if stop is not None:
        return stop
    name = " ".join(str(args.get("payer") or "").split())
    if not name:
        return {"status": "needs", "say": "Say who pays them, like EquipNet or a church's name."}
    said_kind = str(args.get("kind") or "").strip()
    kind = payers.kind_of(said_kind)
    if said_kind and not kind:
        return {
            "status": "needs",
            "say": "A payer is one of these: a support platform (like EquipNet), a sending "
            "church, an agency, or a personal gift. Which is it?",
        }
    form_says, box2 = _dollars_said(args.get("form_says")), _dollars_said(args.get("withheld"))
    said_work = " ".join(str(args.get("ministry_work") or "").strip().lower().split())
    work = {"yes": "yes", "no": "no", "some": "some", "some of each": "some"}.get(said_work)
    year = str(args.get("year") or "").strip()
    if not kind and form_says is None and box2 is None and not work:
        return {
            "status": "needs",
            "say": "Say what kind of payer it is, or what its tax form reports for a year.",
        }
    listed = payers.kept(e for _i, e in _events(t, payers.PAYER_EVENT)).get(unit, [])
    was = next((p for p in listed if p.name.casefold() == name.casefold()), None)
    if not kind and was is None:
        return {
            "status": "needs",
            "say": f"{name} isn't on their list of payers yet. Say what kind of payer it is first.",
        }
    said = {
        "person": unit,
        "name": name,
        "kind": kind or was.kind,
        "words": str(args.get("words") or "").strip() if kind else ";".join(was.words),
        "form": str(args.get("form") or "").strip() if kind else was.form,
        "work": work or (was.work if was else ""),
        "share": str(was.share) if was and was.share and not work else "",
    }
    stop = _form_refused(payers.Payer.of(said), form_says, box2, year, work)
    if stop is not None:
        return stop
    each = None
    if work == "some":
        others = [p for p in listed if p is not was]
        split = _split_said(t, args, payers.Payer.of({**said, "work": ""}), others)
        if isinstance(split, dict):
            return split
        share, each = split
        said["share"] = str(share)
    payer = payers.Payer.of(said)
    jobs = []
    if kind or work:
        jobs.append(
            (
                "payer",
                {
                    "person": unit,
                    "name": name,
                    "kind": payer.kind,
                    "words": ";".join(payer.words),
                    "form": payer.form,
                    "work": payer.work,
                    "share": str(payer.share) if payer.share else "",
                },
            )
        )
    if form_says is not None:
        jobs.append(
            (
                "payer_form",
                {
                    "person": unit,
                    "payer": name,
                    "year": year,
                    "cents": form_says,
                    **({"withheld": box2} if box2 is not None else {}),
                },
            )
        )
    by = t.viewer.person if t.viewer is not None else ""
    for job, params in jobs:
        done = run(
            t.tenant,
            "expenses",
            job,
            params={**params, "by": by},
            ledger_dir=Path(t.receipts.ledger_root).parent,
            tenants_root=t.receipts.tenants_root,
        )
        if done.status == "error":
            return {"status": "busy", "say": "Tim couldn't keep that just now. Try again soon."}
    if form_says is not None:
        others = [p for p in listed if p is not was]
        seen = payers.sort(_their_year(t, unit, year), [*others, payer]).seen(name)
        return {"status": "set", "say": _form_kept(payer, year, form_says, box2, seen)}
    if payer.split and payer.share is not None:
        return {"status": "set", "say": _split_kept(payer, each)}
    if work:
        return {
            "status": "set",
            "say": f"Done: what {name} sends counts as income for their ministry work, with no "
            "tax form, on Schedule C of their tax packet."
            if work == "yes"
            else f"Done: what {name} sends stays a personal gift, kept off their taxes.",
        }
    ask = (
        f" {payers.ASK_WORK}"
        if payer.kind == payers.PERSONAL and not payer.work and payers.looks_like_org(name)
        else ""
    )
    return {"status": "set", "say": f"Done: {name} is on their list of payers, a {kind}.{ask}"}


def _split_said(t: Tools, args: dict, payer, others: list) -> tuple[Fraction, int | None] | dict:
    """How much of each payment from a payer is for the person's ministry
    work, from their own words (dollars of each payment, or a percentage),
    as an exact fraction, with the payment it was said of; or a reply that
    asks again. Code works it out; no model picks a split."""
    name = payer.name
    raw = " ".join(str(args.get("work_part") or "").lower().replace("percent", "%").split())
    each = _dollars_said(args.get("of_each"))
    if each is not None and each <= 0:
        return {"status": "needs", "say": SPLIT_WORDS["each_payment"]}
    if raw.endswith("%"):
        try:
            pct = Decimal(raw.removesuffix("%").strip())
        except InvalidOperation:
            pct = Decimal(-1)
        if not pct.is_finite() or pct < 0 or pct > 100:
            return {"status": "needs", "say": SPLIT_WORDS["how_much"].format(name=name)}
        share = Fraction(pct) / 100
        each = each if each is not None else _newest_from(t, payer, others)
    else:
        part = _dollars_said(raw)
        if part is None or part < 0:
            return {"status": "needs", "say": SPLIT_WORDS["how_much"].format(name=name)}
        each = each if each is not None else _newest_from(t, payer, others)
        if each is None:
            return {"status": "needs", "say": SPLIT_WORDS["no_payment_seen"].format(name=name)}
        if part > each:
            return {"status": "needs", "say": SPLIT_WORDS["too_much"].format(each=_usd(each))}
        share = Fraction(part, each)
    if share <= 0:
        return {"status": "needs", "say": SPLIT_WORDS["none_of_it"]}
    if share >= 1:
        return {"status": "needs", "say": SPLIT_WORDS["all_of_it"]}
    return share, each


def _newest_from(t: Tools, payer, others: list) -> int | None:
    """The newest deposit of the person's own money Tim saw from a payer,
    this year or last, in cents; None when none came yet."""
    from ..agents.expenses import payers
    from ..agents.expenses.statements import THEIRS_IN

    unit, year = payer.person, t._today().year
    seen = [
        r
        for y in (year, year - 1)
        for r in _their_year(t, unit, str(y))
        if r.get("role") == THEIRS_IN
        and not r.get("gift_from")
        and r.get("dollars")
        and Decimal(r["dollars"]) > 0
        and getattr(payers.match(str(r.get("words") or ""), [*others, payer]), "name", "")
        == payer.name
    ]
    if not seen:
        return None
    return int(Decimal(max(seen, key=lambda r: r["date"])["dollars"]) * 100)


def _usd(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def _split_kept(payer, each: int | None) -> str:
    """Both parts said back with their percentages: of the payment it was
    said of, when there is one."""
    from ..agents.expenses import payers

    share, rest = payers.percent(payer.share)
    said = {"name": payer.name, "share": share, "rest": rest}
    if each is None:
        return SPLIT_WORDS["kept_no_payment"].format(**said)
    work, gift = payer.parts(f"{Decimal(each) / 100:.2f}")
    return SPLIT_WORDS["kept"].format(
        each=_usd(each), work=f"${Decimal(work):,.2f}", gift=f"${Decimal(gift):,.2f}", **said
    )


def _dollars_said(said: object) -> int | None:
    """Cents from dollars as said ("1,000.00", "$50"); -1 when they aren't
    dollars; None when nothing was said."""
    raw = str(said or "").replace("$", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return -1
    return int(amount * 100) if amount >= 0 and amount == amount.quantize(Decimal("0.01")) else -1


def _form_refused(payer, form_says: int | None, box2: int | None, year: str, work) -> dict | None:
    """Why a form's figures or a gift's answer can't be kept as said, or None."""
    from ..agents.expenses import payers

    if work and payer.kind != payers.PERSONAL:
        return {
            "status": "needs",
            "say": f"{payer.name} is a {payer.kind}, so what it pays is already their pay. Only "
            "a personal gift is asked whether it comes for their ministry work.",
        }
    if box2 is not None and form_says is None:
        return {
            "status": "needs",
            "say": "Say the W-2's wages from box 1 with the tax withheld from box 2.",
        }
    if form_says is None:
        return None
    if form_says < 0 or (box2 is not None and box2 < 0):
        return {"status": "needs", "say": "Say the form's dollars, like 10,517.98."}
    if not re.fullmatch(r"\d{4}", year):
        return {"status": "needs", "say": "Say which year's form it is, like 2026."}
    if box2 is not None and payer.form != payers.W2:
        return {
            "status": "needs",
            "say": f"Tax withheld comes on a W-2, and {payer.name} sends a "
            f"{payer.form or 'no form'}. Keep the form's box 1 alone.",
        }
    return None


def _form_kept(payer, year: str, cents: int, box2: int | None, seen: int) -> str:
    """What Tim says once a payer's form is kept, checked against what came in."""
    from ..agents.expenses import payers

    said = f"${Decimal(cents) / 100:,.2f}"
    if payer.form == payers.W2:
        held = f", with ${Decimal(box2) / 100:,.2f} federal income tax withheld" if box2 else ""
        return (
            f"Done: {payer.name}'s {year} W-2 says wages of {said}{held}. The tax packet "
            "carries both."
        )
    if cents < seen:
        return (
            f"Done: kept as the form says it. {payer.name}'s {year} form says {said}, "
            f"${Decimal(seen - cents) / 100:,.2f} less than Tim saw arrive. Ask {payer.name} for "
            "a corrected form before it's filed."
        )
    return (
        f"Done: {payer.name}'s {year} form says {said}. The tax packet checks it against "
        "what came in."
    )


def _their_year(t: Tools, unit: str, year: str) -> list[dict]:
    """The person's bank rows for a year, as month_report shows them."""
    return [
        r for person, bank in _year_bank(t, year).items() if unit_of(person) == unit for r in bank
    ]


def _tax_packet(t: Tools, args: dict) -> dict:
    """A missionary's tax packet for a year (c33, payers.py): what each
    payer paid that Tim saw arrive, checked against what its tax form says;
    personal gifts kept off it; their own money that names no payer, and
    deposits still counted as the ministry's that carry a payer's name, for
    the person to say. Read only; the page and the PDF come from
    ``engine tax-packet``."""
    from ..agents.expenses import church_pay, drives, own_pay, packet, payers
    from ..engine.config import load_tenant
    from .tax_facts import reader, said_back

    unit, stop = reader(t, args)  # the person, or whom they shared it with (c38)
    if stop is not None:
        return stop
    year = _year_of(t, args)
    if year is None:
        return {"status": "needs", "say": "Say the year, like 2026."}
    listed = payers.kept(e for _i, e in _events(t, payers.PAYER_EVENT)).get(unit, [])
    said = [e for _i, e in _events(t, payers.FORM_EVENT)]
    everyone = _year_bank(t, year)
    mine = [r for person, bank in everyone.items() if unit_of(person) == unit for r in bank]
    # The ministry's own account is often not theirs to see (a church's
    # treasurer keeps it); the lines that paid them as pay are theirs.
    ministry_bank = everyone if t.viewer is None else _year_bank(replace(t, viewer=None), year)
    pay = church_pay.paid_as_pay((r for bank in ministry_bank.values() for r in bank), unit, year)
    tenant = load_tenant(t.tenant)
    ministry = tenant.identity.legal_name or t.tenant
    listed, got = payers.sort_with_pay(mine, listed, (ministry, pay), unit)
    driven = drives.of_year(
        (e for _i, e in _events(t, DRIVE_EVENT)),
        {str(e.get("drive")): e for _i, e in _events(t, PURPOSE_EVENT)},
        tenant.institution,
        year,
        lambda person: unit_of(person) == unit,
    )
    receipts = own_pay.of_year(_log(t), year, lambda person: unit_of(person) == unit)
    out_payers, says = payers.rows(
        listed, got, payers.figures(said), payers.withheld(said), (unit, year)
    )
    if not listed:
        says.append("No payers are on their list yet: ask who pays them.")
    if got.unmatched:
        n = len(got.unmatched)
        says.append(f"{n} deposit{'' if n == 1 else 's'} of their own name no payer: ask who paid.")
    if dated := [a for a in got.asks if "date" in a]:
        n = len(dated)
        says.append(
            f"{n} deposit{'' if n == 1 else 's'} carr{'ies' if n == 1 else 'y'} a payer's name "
            "but count as the ministry's: ask."
        )
    for a in got.asks:
        if "date" not in a:
            says.append(f"Ask about {a['payer']}: {a['ask']}")
    told = said_back(t, unit, year)
    return {
        "year": year,
        "person": display_name(unit),
        "as_of": t._today().isoformat(),
        "payers": out_payers,
        "personal_gifts": got.gifts,
        "unmatched": got.unmatched,
        "asks": got.asks,
        "what_they_said": told,
        **packet.taxes(
            year, out_payers, mine, says, told, t._today(), drives=driven, receipts=receipts
        ),
        "says": f"{year}: "
        + ("" if told.get("support_elsewhere") else _ASK_SUPPORT)
        + " ".join(says),
    }


_ASK_SUPPORT = (
    "Before any numbers, ask once whether money for their work reaches them anywhere Tim "
    "doesn't see (board members or family sending it straight to them, a platform paying "
    "another account); keep the answer with keep_tax_facts support_elsewhere, and each one "
    "who sends it with keep_payers. "
)
"""Persona night Y07 (2026-11-16): a zero-tax packet given first, and the
support Tim couldn't see found three turns later."""


def _sets_pay(t: Tools) -> bool:
    """Whether to offer ``set_pay``: a church (the small-nonprofit shape,
    the sender model's home until a real church asks for more, c34) and a
    person who decides its rules."""
    from ..engine.config import load_tenant

    if not _decides_rules(t):
        return False
    try:
        tenants_root = t.receipts.tenants_root if t.receipts is not None else None
        return load_tenant(t.tenant, tenants_root=tenants_root).identity.shape == "nonprofit-small"
    except Exception:  # noqa: BLE001  (a tenant that won't load offers nothing new)
        return False


def _set_pay(t: Tools, args: dict) -> dict:
    """The board's pay for one person, as the board decided it: Tim drafts
    the resolution (the policy agent's pay job) and raises its card."""
    from ..engine.runner import door_run as run
    from ..policy import money_words
    from ..policy.pay import ordinal

    me = t.viewer.person if t.viewer is not None else ""
    params = {
        "person": me,
        "for": str(args.get("person") or ""),
        "yearly": str(args.get("yearly") or ""),
        "day": str(args.get("day") or ""),
        "from": str(args.get("from") or ""),
    }
    where = {
        "ledger_dir": Path(t.receipts.ledger_root).parent,
        "tenants_root": t.receipts.tenants_root,
    }
    wrote = run(t.tenant, "policy", "pay", params=params, **where)
    if wrote.status not in ("ok", "replayed"):
        why = wrote.summary.removeprefix("policy pay: ")
        return {"status": "refused", "say": why[:1].upper() + why[1:].rstrip(".") + "."}
    run(t.tenant, "policy", "propose", **where)
    policy = t.viewer.policy if t.viewer is not None else None
    found = policy.person_named(params["for"]) if policy is not None else None
    name = policy.display(found) if policy is not None and found else params["for"]
    yearly = Decimal(params["yearly"].replace("$", "").replace(",", "").strip())
    start = datetime.strptime(params["from"], "%Y-%m")
    return {
        "status": "drafted",
        "say": (
            f"Drafted the board's resolution: {name} is paid {money_words(yearly)} a year, "
            f"on the {ordinal(int(params['day']))} of each month, {start:%B %Y} through "
            f"December {start:%Y}. It waits in waiting_cards for the yes of a director with no "
            f"family tie to {name}. Neither {name} nor {name}'s family can adopt it. Nothing "
            "is paid until then, and Tim never sends the money: on each pay day Tim tells "
            "whoever pays the ministry's bills, and the bank line shows it was paid."
        ),
    }


SET_PAY = ToolSpec(
    "set_pay",
    "Set a missionary's pay the way the board decided it: a yearly amount, the day of the "
    "month it is paid, and the month it starts (it runs through that December; the board "
    "sets it again each year, and a lower amount from a later month is a new resolution). "
    "Call it when a board member tells you what the board decided about someone's pay. "
    "Tim drafts the resolution and it waits for a director with no family tie to the person "
    "it pays. Nothing is paid here.",
    _schema(
        {
            "person": {"type": "string", "description": "Who is paid, by name."},
            "yearly": {"type": "string", "description": "The pay for a full year, in dollars."},
            "day": {"type": "integer", "description": "The day of the month it's paid, 1 to 28."},
            "from": {"type": "string", "description": "The month it starts, like 2027-01."},
        },
        required=["person", "yearly", "day", "from"],
    ),
    _set_pay,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Set a missionary's pay the way the board decided it, when a board member says so: a "
        "yearly amount, the day of the month it is paid, and from, the month it starts (it runs "
        "through that December; a lower amount from a later month is a new resolution). Tim "
        "drafts the resolution and it waits for a director with no family tie to the person it "
        "pays. Nothing is paid here."
    ),
)
"""c34: a church's door, to whoever decides its rules; writes a draft, decides nothing."""


KEEP_PAYERS = ToolSpec(
    "keep_payers",
    "Keep who pays a missionary, in their own words: the payer's name and its kind (a "
    "support platform like EquipNet, a sending church, an agency, or a personal gift), the "
    "words the bank prints on its deposits when they differ from its name (words; several "
    "joined by ;), and form W-2 for an agency that employs them. When they read a payer's tax "
    "form, keep what it says right away (payer with year and form_says; a W-2's box 1 wages "
    "with withheld, its box 2), even when it differs from what came in: the packet checks it "
    "and names the difference, so never hold it back to compare first. A personal gift Tim "
    'asks about ("Do they send this because of your ministry work?") takes their answer in '
    "ministry_work: yes, no, or some when they send some of each; with some, work_part is "
    "how much of each payment is for their ministry work in their own words, dollars like "
    "150 (of_each, the payment it's part of, when Tim hasn't seen one) or a percentage like "
    "30%, and Tim splits every deposit from them the same way. A one-time gift for them is "
    "no payer: name its giver on that deposit with "
    "explain_bank_line's gift_from. person names the missionary when whoever looks over every "
    "month is the one saying it. The newest word on each payer stands. It moves no money.",
    _schema(
        {
            "payer": {"type": "string", "description": "Who pays them, like EquipNet."},
            "kind": {
                "type": "string",
                "description": "support platform, sending church, agency, or personal gift.",
            },
            "words": {"type": "string", "description": "What the bank prints on its deposits."},
            "form": {"type": "string", "description": "W-2, for an agency that employs them."},
            "year": {"type": "string", "description": "The form's year, like 2026."},
            "form_says": {
                "type": "string",
                "description": "The form's dollars as printed, like 10517.98 (a W-2's box 1).",
            },
            "withheld": {
                "type": "string",
                "description": "A W-2's box 2, federal income tax withheld, like 2100.00.",
            },
            "ministry_work": {
                "type": "string",
                "enum": ["yes", "no", "some"],
                "description": "Whether a personal gift comes because of their ministry work.",
            },
            "work_part": {"type": "string", "description": "With some: like 150, or 30%."},
            "of_each": {"type": "string", "description": "Payment work_part is of."},
            "person": {"type": "string", "description": "The missionary's name."},
        }
    ),
    _keep_payers,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Keep who pays a missionary: payer and kind (support platform, sending church, agency, "
        "or personal gift), words its bank deposits carry if not its name (joined by ;), form "
        "W-2 for an agency employing them. Keep a tax form's figures now (year, form_says; "
        "withheld: W-2 box 2), even if they differ from what came in: never hold it back to "
        "compare first. ministry_work: yes, no, or some, whether a personal gift comes for "
        "their ministry work; with some, work_part: the part of each payment for it. A "
        "one-time gift is no payer: use explain_bank_line's gift_from. person: the missionary, "
        "for their reviewer. Newest word stands."
    ),
)
"""A missionary's payers (c33): any door, for the person and whoever looks over every month."""


TAX_PACKET = ToolSpec(
    "tax_packet",
    "A missionary's tax packet for a year (leave year out for this year): each payer, its "
    "kind and the tax form it sends, what Tim saw it pay and on which days, and what its "
    "form says once kept with keep_payers (not_seen went to an account Tim doesn't see; a "
    "W-2 shows its wages and tax withheld). When they tell you what a form says, keep it "
    "with keep_payers before anything else, even when it looks wrong. Personal gifts are "
    "listed and kept off it. unmatched: their own money that names no payer (ask who paid; "
    "a one-time gift for them names its giver with explain_bank_line's gift_from); asks: "
    "deposits counted as the ministry's that carry a payer's name, and gifts that may come "
    "for their ministry work (ask, in Tim's words). Then the tax half: schedule_c (1099 "
    "income and gifts for their work with no form, its costs, the net), "
    "self_employment_tax (never lowered by the Foreign Earned Income Exclusion), days_abroad "
    "(full days counted from the travel dates kept with keep_travel, the best 12 months in a "
    "row against the 330 needed, with days_still_ahead when it counts days not yet lived, and "
    "each day the bank shows spending abroad when they said they were home, to ask about; "
    "with no travel dates, a draft from the bank's foreign spending and its long gaps), and "
    "federal_tax: the return worked out to a bracket (taxable income, income tax, "
    "top_bracket, total with self-employment tax, withholding, estimates already paid, "
    "next year's four estimates), without_exclusion always and with_exclusion beside it when "
    "their travel dates qualify; never pick one for them, the choice is theirs with their "
    "preparer. part_year_through: asked before the year ends, it's the year so far. "
    "left_to_preparer: what Tim doesn't work out (state income tax and the minister's "
    "housing allowance among them: say so in a line, and that their preparer settles it). "
    "what_they_said: their filing status, a spouse's wages, last year's tax, estimates paid, "
    "and trips, as kept with keep_tax_facts and keep_travel. A packet is the missionary's "
    "and their ministry's reviewer's; person names the missionary (also one who shared "
    "theirs with the person asking). Start with "
    "the says line; quote the dollars as given. Read only.",
    _schema(
        {
            "year": {"type": "string", "description": "Like 2026."},
            "person": {"type": "string", "description": "The missionary's name."},
        }
    ),
    _tax_packet,
    short=(
        "A missionary's tax packet for a year (year left out: this year; person: the missionary, "
        "for their reviewer). Each payer, what Tim saw it pay, and what its form says (kept with "
        "keep_payers); personal gifts are listed and kept off it. When they say what a form says,"
        " keep it with keep_payers first, even when it looks wrong. unmatched: their own money "
        "that names no payer (ask who paid; a one-time gift for them names its giver with "
        "explain_bank_line's gift_from). asks: what to ask them, in your words. The tax half: "
        "schedule_c, self_employment_tax (never lowered by the Foreign Earned Income Exclusion), "
        "days_abroad (from the trips kept with keep_travel, against the 330 needed; ask about "
        "each day the bank shows spending abroad when they said they were home), and federal_tax:"
        " without_exclusion, and with_exclusion when their trips qualify; never pick one for "
        "them, the choice is theirs with their preparer. part_year_through: the year so far. "
        "left_to_preparer: say in a line that their preparer settles it. Start with the says "
        "line. Read only."
    ),
)
"""The tax packet (c33): any door, for the person, their reviewer, and whom they share it with."""
