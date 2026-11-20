"""What a person's month is made of: the unconsumed receipts, the drives, the
bank lines, their prices (foreign ones at the recorded rate), and each
line's words on a report. The report, month, send, and line jobs read it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ...engine.contracts import EventSpec, JobContext
from ..policy.jobs import rules_in_force
from . import categories as cats
from . import fx, records, statements
from .common import _events_of, _landed_by_sha, _proposals_by_sha, _reference_shas
from .records import (
    AMOUNT_EVENT,
    CATEGORY_EVENT,
    DRIVE_EVENT,
    PURPOSE_EVENT,
    drive_id,
)
from .schema import (
    parse_amount_tag,
    parse_foreign_claim,
)


def _consumed_shas(ctx: JobContext) -> set[str]:
    rows = ctx.ledger.conn.execute(
        "SELECT receipt_sha256 FROM expense_line WHERE tenant = ?", (ctx.tenant_slug,)
    ).fetchall()
    return {str(r[0]) for r in rows if r[0]}


# ---- job: report ------------------------------------------------------------


def _unconsumed(ctx: JobContext) -> list[dict]:
    """Landed receipts no manifest has consumed and no reference event
    reclassified, joined to their proposals when one exists. A door receipt
    whose amount the person said after the photo carries it as
    ``claim_name``, the name it would have had with the claim in it."""
    consumed = _consumed_shas(ctx) | _reference_shas(ctx)
    proposals = _proposals_by_sha(ctx)
    log = ctx.ledger.read_event_log()
    said = amounts_said(log)
    dated = records.days(log)
    out = []
    for sha, landed in sorted(_landed_by_sha(ctx).items()):
        if sha in consumed:
            continue
        item = dict(landed)
        item.update(proposals.get(sha, {}))
        item["sha256"] = sha
        name = str(landed.get("file", ""))
        if sha in dated:
            # A receipt sent later is filed by its own day (t-catch-up):
            # its month, its line, and its rate are that day's.
            name = records.dated_name(name, dated[sha])
            item["month"] = dated[sha][:7]
        elif records.UNDATED in name:
            item["month"] = ""  # in no month until its day is known
        if sha in said:
            item["claim_name"] = claim_name(name, said[sha])
        elif name != landed.get("file"):
            item["claim_name"] = name
        out.append(item)
    return out


def _drive_items(ctx: JobContext, month: str, person: str = "") -> list[dict]:
    """The month's ministry drives not yet in a report, each a line priced at
    the rate in force on its own day (the owner, 2026-10-14). A drive the
    person called personal, or hasn't spoken for, never becomes money; one
    that can't be priced (``drives.priced``) waits off the report."""
    from .drives import priced

    said = {str(e["payload"].get("drive")): e["payload"] for e in _events_of(ctx, PURPOSE_EVENT)}
    consumed = _consumed_shas(ctx)
    listed = list(ctx.tenant.expenses.category_accounts)
    category = cats.canonical("Mileage", listed) or cats.canonical("Travel", listed) or ""
    out = []
    for e in _events_of(ctx, DRIVE_EVENT):
        drive = e["payload"]
        day = str(drive.get("start", ""))[:10]
        word = said.get(drive_id(drive), {})
        sha = records.drive_sha(drive_id(drive))
        if day[:7] != month or not word.get("ministry") or sha in consumed:
            continue
        if person and drive.get("person") != person:
            continue
        miles = Decimal(str(drive.get("miles", "0")))
        money = priced(miles, ctx.tenant.institution, day)
        if "refused" in money:
            continue
        words = str(word.get("said", "")).replace(";", ",")
        out.append(
            {
                "sha256": sha,
                "file": f"{day} drive, {miles} miles",
                "drive": True,
                "person": str(drive.get("person", "")),
                "month": month,
                "expense_date": day,
                "miles": str(miles),
                "rate": str(money["rate"]),
                "rate_from": money["rate_from"],
                "note": words,
                "amount_cents": int(money["dollars"] * 100),
                "category": category,
                "project": "",
                "filed_to": "",
                "flags": [],
            }
        )
    return out


def _bank_items(ctx: JobContext, month: str, person: str = "") -> list[dict]:
    """The month's statement lines and school payments that are spending
    (t-statements, the owner's decisions of 2026-10-14), each a line of the
    person's card: what the person paid for the ministry, owed back, and
    what the ministry paid itself, on the card for the reviewer's yes but
    never owed. A line still waiting on its receipt or its purpose stays off
    the card until it has one; money that only moved is never a line, and
    neither is a payment service's fee on a gift (c32)."""
    listed = list(ctx.tenant.expenses.category_accounts)
    log = ctx.ledger.read_event_log()
    out = []
    for ln in statements.month_lines(log, month, reported=_consumed_shas(ctx), categories=listed):
        if ln["role"] not in statements.SPENDING or ln["reported"]:
            continue
        if ln.get("processor"):  # a payment service's own fee: no one's yes (c32)
            continue
        if ln["waiting_on"] == statements.WAITING or (person and ln["person"] != person):
            continue
        item = {
            "sha256": ln["sha256"],
            "file": f"{ln['day']} {ln['words']}",
            "bank": True,
            "paid_by": "ministry" if ln["role"] == statements.MINISTRY_PAID else "person",
            "person": ln["person"],
            "month": month,
            "expense_date": ln["day"],
            "vendor": ln["words"],
            "note": ln["words"],
            "amount_cents": ln["cents"],
            "category": ln["category"],
            "project": "",
            "filed_to": ln["receipt_file"],
            "flags": [],
        }
        if (ln.get("school") or ln.get("cash_used")) and ln.get("said_currency"):
            if ln["cents"] is None:
                # No rate for its day yet: the claim goes on so pricing asks
                # the feed, and the line waits rather than guess.
                item |= {"claim_name": ln["claim_name"], "amount_cents": None}
            else:
                # month_lines priced it, once, the way the door reads it: the
                # card and the report carry that, never a second pricing
                # (2026-11-10: every part of a split was priced at the whole).
                item |= {k: v for k, v in ln.items() if k.startswith(("said_", "fx_"))}
                if ln.get("owed_cents"):
                    item["owed_cents"] = ln["owed_cents"]
        if ln.get("part_no"):
            item["part"] = ln["part_no"]
        out.append(item)
    return out


def _with_statements(ctx: JobContext, month: str, person: str, mine: list[dict]) -> list[dict]:
    """A person's month with its statement lines: a receipt standing behind
    a line rides that line, never a line of its own; drives and lines join."""
    behind = statements.held(ctx.ledger.read_event_log())  # riding a line, or asked about
    return [
        *(i for i in mine if str(i.get("sha256")) not in behind),
        *_drive_items(ctx, month, person),
        *_bank_items(ctx, month, person),
    ]


def _is_door_receipt(name: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}_receipt_", name))


_TAG = re.compile(r"^(?:\$\d+\.\d{2}|[A-Z]{3}\d+\.\d{2})$")


def amounts_said(events: list[dict], kind: str = AMOUNT_EVENT) -> dict[str, str]:
    """Each door receipt's latest amount tag of ``kind`` ("$43.00",
    "NPR4300.00"): the person's words (``AMOUNT_EVENT``) or a photo's
    reading (``READ_EVENT``)."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"]["tag"])
        for e in events
        if e.get("event_type") == kind and e.get("payload", {}).get("sha256")
    }


def claim_name(name: str, tag: str) -> str:
    """A door receipt's name as if the claim had been said with it, for the
    pricing that reads claims from names."""
    return f"{name[:10]}_receipt_{tag}{Path(name).suffix}"


def _name_of(item: dict) -> str:
    return str(item.get("claim_name") or item.get("file", ""))


def _fx_inputs(ctx: JobContext, items: list[dict]) -> tuple[list[str], str]:
    """What a foreign receipt's price reads, for a run key: the rates
    already recorded for these receipts, and today's date while any rate is
    the time while any rate is still pending (so a pending rate is asked
    for again on the next run, never skipped as an idempotent re-run)."""
    log = ctx.ledger.read_event_log()
    rates = fx.stored(log)
    drawn, _on_hand = statements.cash(log)
    used, pending = [], False
    for i in items:
        claim = parse_foreign_claim(_name_of(i))
        if claim is None:
            continue
        day, _amount, currency = claim
        if (d := drawn.get(str(i.get("sha256")))) is not None:
            used.append(
                f"cash:{d.atm_day}:{d.cents}:{d.ministry}"
                + (f":{d.own_cents}" if d.own_cents else "")
            )
        elif (currency, day) in rates:
            used.append(fx.rate_key(currency, day))
        else:
            pending = True
    asked = datetime.now(UTC).isoformat() if pending else ""
    return sorted(set(used)), asked


def _rate_once_known(ctx: JobContext, event: EventSpec) -> list[EventSpec]:
    """The day's rate for a receipt in another currency, recorded the moment
    ``event`` gives it both its day and its amount (the dress rehearsal,
    2026-10-15). A rupee receipt paid by the ministry's card finds its card
    line only at the day's rate; fetched only when the month was sent, the
    card line waited on its receipt, the month would not send, and the
    receipt counted as cash. The month job still asks for any rate missing."""
    log = [*ctx.ledger.read_event_log(), {"event_type": event.event_type, "payload": event.payload}]
    sha = str(event.payload.get("sha256", ""))
    wants = {(c, d) for r, d, c, _a in statements.foreign_receipts(log) if r == sha}
    return _rates_for(wants, fx.stored(log))


def _rates_for(wants: set[tuple[str, str]], rates: dict) -> list[EventSpec]:
    """A rate event for each (currency, day) not recorded yet that the feed has."""
    out = []
    for currency, day in sorted(wants - set(rates)):
        rate = fx.fetch(currency, day)
        if rate is not None:
            out.append(EventSpec(key=rate.key(), event_type=fx.RATE_EVENT, payload=rate.payload()))
    return out


def _price_claims(ctx: JobContext, items: list[dict]) -> tuple[list[dict], list[EventSpec]]:
    """Price door receipts from the person's own words (alpha step 3). A
    claim in dollars is the amount when nothing else priced the receipt; a
    claim in another currency is always priced at the rate on the
    receipt's own date (fx.priced), recorded once and kept on the line. With
    no rate yet, the line stays unpriced and says so; nothing is guessed."""
    log = ctx.ledger.read_event_log()
    rates = fx.stored(log)
    drawn, _on_hand = statements.cash(log)
    events: list[EventSpec] = []
    for want in sorted({w for i in items if (w := fx.needed(_name_of(i)))}):
        if want in rates:
            continue
        rate = fx.fetch(*want)
        if rate is not None:
            rates[want] = rate
            events.append(
                EventSpec(key=rate.key(), event_type=fx.RATE_EVENT, payload=rate.payload())
            )
    out = []
    for item in items:
        name = _name_of(item)
        if not _is_door_receipt(name):
            out.append(item)
            continue
        item = dict(item)
        flags = [str(f) for f in item.get("flags", [])]
        cash = drawn.get(str(item.get("sha256")))
        fields = fx.priced(name, rates, cash)
        if cash is not None and cash.ministry:
            item["paid_by"] = "ministry"  # the ministry's own cash: nothing owed back
            if cash.own_cents:  # but for their own cash, when it paid the rest (10/16)
                item["owed_cents"] = cash.own_cents
        if not fields:
            said = parse_amount_tag(name)
            if not item.get("amount_cents") and said is not None:
                item["amount_cents"] = said
                flags.append("amount as the person said it")
        else:
            read = item.get("amount_cents")
            pending = fields.pop("rate_pending", False)
            item.update(fields)
            currency, day = fields["said_currency"], fields["fx_date"]
            if pending:
                flags.append(
                    f"{currency} {Decimal(fields['said_amount']):,.2f}: rate pending for {day}"
                )
            elif read and int(read) != fields["amount_cents"]:
                flags.append(f"the photo read {int(read) / 100:,.2f}; the person said {currency}")
        item["flags"] = flags
        out.append(item)
    return out, events


def owed_back(i: dict) -> int:
    """What a card line owes back to the person: all of it when they paid,
    nothing when the ministry did, and only their part when the ministry's
    cash ran short and theirs paid the rest."""
    if i.get("paid_by") == "ministry":
        return int(i.get("owed_cents") or 0)
    return int(i["amount_cents"])


def _manifest_note(i: dict) -> str:
    note = str(i["note"]) if i.get("drive") or i.get("bank") else ""
    if i.get("own_part"):
        note = "; ".join(filter(None, (note, OWN_PART_NOTE)))
    return note


OWN_PART_NOTE = "their own cash; the ministry's cash paid the rest"


def _own_part(i: dict) -> dict:
    """A line the ministry's cash paid first, as the report's line of what
    the person's own cash paid: their part's dollars, said as such, with no
    rate (the rate priced the whole purchase)."""
    part = {k: v for k, v in i.items() if k not in _FX_FIELDS}
    return {**part, "amount_cents": int(i["owed_cents"]), "paid_by": "person", "own_part": True}


def _line_words(i: dict, label: str = "", *, funds: int = 2) -> str:
    """One line of a review card, a foreign amount shown as said with its rate.
    A door receipt reads in a reviewer's words: its day, what it was, its
    category ("Uncategorized" when nobody has placed it), and its fund when
    the card's lines span more than one (``funds``; the 10/14 practice run: a
    church with one fund read "General fund" on every line). A drive reads
    its miles, what it was for, and its rate per mile."""
    if i.get("bank"):
        words = f"{i['expense_date']}: ${int(i['amount_cents']) / 100:,.2f}"
        if i.get("said_currency"):
            words += (
                f" ({i['said_currency']} {Decimal(i['said_amount']):,.2f} at "
                f"{Decimal(i['fx_rate']):,.2f} per dollar on {i['fx_date']})"
            )
        paid = f", {statements.MINISTRY_PAID}" if i.get("paid_by") == "ministry" else ""
        if paid and i.get("owed_cents"):
            paid += f" (${int(i['owed_cents']) / 100:,.2f} of it their own cash, owed back)"
        return f"{words}, {i['note']}, {i.get('category') or cats.UNCATEGORIZED}{paid}"
    if i.get("drive"):
        cents = f"{Decimal(i['rate']) * 100:f}".rstrip("0").rstrip(".")
        return (
            f"{i['expense_date']}: {i['miles']} miles, {i['note']}, "
            f"${int(i['amount_cents']) / 100:,.2f} at {cents} cents a mile"
        )
    if _is_door_receipt(str(i["file"])):
        words = f"{_name_of(i)[:10]}: ${int(i['amount_cents']) / 100:,.2f}"
        if i.get("said_currency"):
            words += (
                f" ({i['said_currency']} {Decimal(i['said_amount']):,.2f} at "
                f"{Decimal(i['fx_rate']):,.2f} per dollar on {i['fx_date']})"
            )
        where = f", {i['project']} {label}".rstrip() if i.get("project") and funds > 1 else ""
        # Said on the reviewer's card (the dress rehearsal, 2026-10-15: unsaid,
        # the reviewer's AI read these as paid by the person and owed back).
        paid = ", from the ministry's cash" if i.get("paid_by") == "ministry" else ""
        if paid and i.get("owed_cents"):
            paid += f" and ${int(i['owed_cents']) / 100:,.2f} of their own, owed back"
        return f"{words}, {i.get('category') or cats.UNCATEGORIZED}{where}{paid}"
    words = f"{i['file']} ${int(i['amount_cents']) / 100:,.2f}"
    if i.get("said_currency"):
        words += (
            f" ({i['said_currency']} {Decimal(i['said_amount']):,.2f} at "
            f"{Decimal(i['fx_rate']):,.2f} per dollar on {i['fx_date']})"
        )
    return f"{words} {i.get('category') or '?'} ({i.get('project') or '?'})"


def _report_content_hash(items: list[dict]) -> str:
    parts = [
        f"{i['sha256']}:{i.get('amount_cents')}:{i.get('category')}:{i.get('project')}"
        + (f":{i['claim_name']}" if i.get("claim_name") else "")
        + (f":{i['note']}" if i.get("drive") or i.get("bank") else "")  # words on its line
        + (f":{i['paid_by']}" if i.get("bank") else "")
        for i in items
    ]
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()


_FX_FIELDS = ("said_amount", "said_currency", "fx_rate", "fx_date", "fx_source")


def _rules_in_force_key(ctx: JobContext) -> list[str] | None:
    """The fingerprints of the ministry's rules in force, or None when it has
    none (its run keys and cards stay exactly as they were)."""
    authority = ctx.authority
    if authority is None or (authority.rules is None and not authority.rules_error):
        return None
    if authority.rules_error:
        return [f"unreadable:{authority.rules_error}"]
    shas = sorted(r.sha for r in rules_in_force(ctx.ledger, ctx.tenant_slug, authority.rules))
    return shas or None


def _categories_by_sha(ctx: JobContext) -> dict[str, str]:
    """The category each receipt was last placed in, by the person's words or a
    reviewer's (``expense.category_set``); later events win."""
    out: dict[str, str] = {}
    for e in _events_of(ctx, CATEGORY_EVENT):
        payload = e.get("payload", {})
        if payload.get("sha256"):
            out[str(payload["sha256"])] = str(payload.get("category", ""))
    return out
