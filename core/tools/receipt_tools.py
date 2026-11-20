"""The receipt tools: sending a receipt, paying for something yourself,
sorting a receipt (its kind, amount, day, drive), reading receipts, and
sending a month for review. Each answers through core/tools/receipts.py.
Moved out of core/tools/catalog.py.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.expenses import advances, statements
from ..agents.expenses.categories import canonical, from_words
from .reads import _advances, _name_of
from .spec import ToolSpec, _schema

if TYPE_CHECKING:
    from .catalog import Tools


def _paid_it_myself(t: Tools, args: dict) -> dict:
    """A ministry expense the signed-in person paid from their own pocket,
    one time (c28): a card someone else says yes to; then the books owe it
    back to them until the bank's line paying them back is explained."""
    from ..engine.config import load_tenant
    from ..engine.runner import door_run as run

    person = t.viewer.person if t.viewer is not None else ""
    day = str(args.get("day") or "").strip()
    said = str(args.get("said") or "").strip()
    try:
        amount = Decimal(str(args.get("amount") or "").replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        amount = Decimal(0)
    if not person or not said or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        return {
            "status": "needs",
            "say": "Ask what they paid for, the day, and the dollars, in their words.",
        }
    try:
        when = date.fromisoformat(day)
    except ValueError:
        return {"status": "needs", "say": "Say the day they paid it, as YYYY-MM-DD."}
    if when > t._today():
        return {"status": "needs", "say": "That day hasn't come yet. Ask which day they paid it."}
    listed = list(load_tenant(t.tenant).expenses.category_accounts)
    asked = str(args.get("category") or "").strip()
    category = canonical(asked, listed) if asked else from_words(said, listed)
    if category is None:
        return {
            "status": "needs_category",
            "say": f"The ministry's categories are {', '.join(listed)}. Ask which one fits, "
            "then pass it as category.",
        }
    cents = int(amount * 100)
    if args.get("again") is not True and (kept := _kept(t, person, day, cents)) is not None:
        return kept
    done = run(
        t.tenant,
        "expenses",
        "advance",
        params={
            "person": person,
            "name": _name_of(t, person),
            "day": day,
            "cents": str(cents),
            "said": said,
            "category": category or "",
            "by": person,
        },
        ledger_dir=Path(t.ledger_root).parent,
        tenants_root=t.receipts.tenants_root if t.receipts is not None else None,
    )
    card = next(
        (
            r
            for r in t._rows(advances.PENDING, (t.tenant, advances.ADVANCE_CARD))
            if json.loads(r["params_json"] or "{}").get("person") == person
            and json.loads(r["params_json"] or "{}").get("cents") == str(cents)
            and json.loads(r["params_json"] or "{}").get("day") == day
        ),
        None,
    )
    if done.status == "error" or card is None:
        if any(
            c["person"] == person and c["day"] == day and c["cents"] == str(cents)
            for c in _advances(t)
        ):
            return {"status": "approved", "say": "That one is already approved and owed back."}
        return {"status": "busy", "say": "Tim couldn't ask for that just now. Try again soon."}
    own = _may_decide_own(t, person, cents)
    who = (
        f"Their ministry lets them approve it themselves: on their yes in their own words, "
        f"call decide_card with card {card['id']}."
        if own
        else "It waits on a yes from someone else at the ministry who approves spending; "
        "their ministry doesn't let anyone approve their own."
    )
    return {
        "status": "waiting",
        "card": card["id"],
        "say": f"Done: ${amount:,.2f} on {statements.day_words(day)} for {said}"
        + (f", counted as {category}" if category else "")
        + f". {who} Once it's approved the books show it as owed back to them, until the "
        "bank line that pays them back is explained.",
    }


def _kept(t: Tools, person: str, day: str, cents: int) -> dict | None:
    """The same cost already in the books: the same person, day, and dollars,
    waiting on a yes or approved (the voice gate, 2026-11-20, A08: after her yes
    page approved both, Tim recorded the $18.00 again, worded differently,
    and the copy sat waiting). A second one goes in only on ``again``."""

    def same(params: dict) -> bool:
        return (
            params.get("person") == person
            and params.get("day") == day
            and str(params.get("cents")) == str(cents)
        )

    dollars = f"${cents / 100:,.2f} on {statements.day_words(day)}"
    for r in t._rows(advances.PENDING, (t.tenant, advances.ADVANCE_CARD)):
        if same(json.loads(r["params_json"] or "{}")):
            return {
                "status": "already",
                "card": r["id"],
                "say": f"Already recorded: {dollars}, waiting on a yes. Nothing new was "
                "recorded. Only if they say they paid a second one, call again with again true.",
            }
    if any(same(c) for c in _advances(t)):
        return {
            "status": "already",
            "say": f"Already recorded and approved: {dollars}, owed back to them. Nothing new "
            "was recorded. Only if they say they paid a second one, call again with again true.",
        }
    return None


def _may_decide_own(t: Tools, person: str, cents: int) -> bool:
    """Whether the ministry's own safeguards let ``person`` approve the
    expense they paid themselves (the owner, 2026-11-12)."""
    from ..authority import card_request, evaluate

    if t.viewer is None:
        return False
    policy = t.viewer.policy
    params = {"person": person, "cents": str(cents)}
    return evaluate(policy, card_request(advances.RULE, person, params, policy)).allowed


PAID_IT_MYSELF = ToolSpec(
    "paid_it_myself",
    "Record a ministry expense the signed-in person paid from their own pocket, one time "
    "(a board member who paid a state filing fee on their own card, say): the day "
    "(YYYY-MM-DD), the dollars, and what it was for, all from their own words, and the "
    "category from the ministry's list when they name one. It raises a card, approved as "
    "the ministry's own rules say: at a small ministry by the person themselves, at a church "
    "or an organization by someone else; the reply says which. Once approved it is the "
    "ministry's spending, owed "
    "back to them, until the bank line that pays them back is explained with pays_back. "
    "It moves no money.",
    _schema(
        {
            "day": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "amount": {"type": "string", "description": "The dollars they said, like 312.50."},
            "said": {"type": "string", "description": "Their own words: what it was for."},
            "category": {"type": "string", "description": "From the ministry's list."},
            "again": {
                "type": "boolean",
                "description": "True only when they say it is a second cost of the same day "
                "and dollars as one already recorded.",
            },
        },
        ["day", "amount", "said"],
    ),
    _paid_it_myself,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    short=(
        "Record a ministry expense the person paid from their own pocket, one time: day "
        "(YYYY-MM-DD), amount, and what it was for, all from their own words, and category when "
        "they name one. It raises a card, approved as the ministry's own rules say; the reply "
        "says by whom. Once approved it is owed back to them until the bank line that pays them "
        "back is explained with pays_back."
    ),
)
"""An expense paid from someone's own pocket (c28): on the box's door, for anyone."""


def _set_category(t: Tools, args: dict) -> dict:
    return t.receipts.set_category(t, args)


SET_CATEGORY = ToolSpec(
    "set_category",
    "Put one receipt in the month under a category, when the person asks: their own "
    "receipts, or any receipt in a month they review. Find the receipt's line in "
    "month_report and pass its source as line, the category as they said it, and their "
    "words in said. Their word decides it (a category moves no money); a month waiting "
    "for its yes shows the change before anyone approves. A filed month can't change. "
    "When they say they paid a receipt from their own pay and want nothing back, pass "
    'whose "pay" (no category needed): it rides no card, and it is a cost on their tax '
    'packet. whose "ministry" puts it back on the card.',
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "category": {"type": "string"},
            "whose": {
                "type": "string",
                "enum": ["pay", "ministry"],
                "description": "pay: they paid it from their own pay and nobody owes it back. "
                "ministry: the ministry pays it back, as usual.",
            },
            "said": {
                "type": "string",
                "description": "The person's own words asking for the change.",
            },
        },
        ["line", "said"],
    ),
    _set_category,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "Put one receipt under a category when the person asks (their own, or one in a month they"
        " review): line is its source from month_report, category as they said it, said their "
        "words. A filed month can't change. When they paid it from their own pay and want nothing"
        ' back, pass whose "pay" (no category needed): a cost on their tax packet. whose '
        '"ministry" puts it back on the card.'
    ),
)


def _set_drive_purpose(t: Tools, args: dict) -> dict:
    return t.receipts.set_drive_purpose(t, args)


SET_DRIVE_PURPOSE = ToolSpec(
    "set_drive_purpose",
    "Say what one drive was for, when the person tells you: find it in month_report's "
    'drives and pass its source as line, and their own words in said ("visiting the church '
    'in Pokhara", or "that was personal"). A ministry drive is priced at the rate on its '
    "own day; a personal one is kept and never priced. Never guess what a drive was for.",
    _schema(
        {
            "line": {"type": "string", "description": "The drive's source from month_report."},
            "said": {"type": "string", "description": "The person's own words about the drive."},
        },
        ["line", "said"],
    ),
    _set_drive_purpose,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "Say what one drive was for: line is its source from month_report's drives, said their "
        'own words ("visiting the church in Pokhara", or "that was personal"). Never guess what a'
        " drive was for."
    ),
)


def _set_amount(t: Tools, args: dict) -> dict:
    return t.receipts.set_amount(t, args)


SET_AMOUNT = ToolSpec(
    "set_amount",
    "Say how much a receipt was, for one added without an amount: find its line in "
    "month_report and pass its source as line, and the person's own words in said. "
    "Their words set it: the amount they say, or their yes to what the photo reads "
    "(the line's read). Never pass an amount they didn't say or agree to.",
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "said": {
                "type": "string",
                "description": "The person's own words: the amount, or their yes to the reading.",
            },
        },
        ["line", "said"],
    ),
    _set_amount,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "How much a receipt added without an amount was: line is its source from month_report, "
        "said the person's own words (an amount they say, or their yes to what the photo reads). "
        "Never pass an amount they didn't say or agree to."
    ),
)


def _add_receipt(t: Tools, args: dict) -> dict:
    return t.receipts.add(str(args.get("said") or ""), t, another=bool(args.get("another")))


ADD_RECEIPT = ToolSpec(
    "add_receipt",
    "Add one of the signed-in person's own receipts, when they say they have one; pass "
    "their words, exactly as they said them, in said: the amount and currency are read "
    "from those words, never from you. The reply is a link: show its show line as given; "
    "their tap opens their phone's camera and the photo goes in with this month's "
    "receipts. With no amount in their words the link still works; ask how much after the "
    "photo and pass their words to set_amount. When Tim already keeps a receipt of theirs "
    "at that amount, the reply names it instead of a link.",
    _schema(
        {
            "said": {
                "type": "string",
                "description": "The person's own words about the receipt, exactly as they "
                "said them; never your summary of them.",
            },
            "another": {
                "type": "boolean",
                "description": "True only when they say this is a new receipt, not the one "
                "Tim already keeps at that amount.",
            },
        },
        ["said"],
    ),
    _add_receipt,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    short=(
        "Add one of the person's own receipts when they say they have one; pass their words "
        "exactly as they said them in said (the amount is read from their words, never from you)."
        " The reply is a link that opens their camera: show its show line as given. With no "
        "amount in their words, ask how much after the photo and pass their words to set_amount. "
        "When Tim already keeps a receipt of theirs at that amount, the reply names it instead of"
        " a link."
    ),
)


def _catch_up_link(t: Tools, args: dict) -> dict:
    return t.receipts.catch_up(t, args)


CATCH_UP_LINK = ToolSpec(
    "catch_up_link",
    "One link for a whole pile of receipts, when someone is catching up (a month or a "
    "year of them: photos, PDFs, bank statements, a school list): the signed-in "
    "person's own, or pass person to get the link for someone whose month they review. "
    "Show its show line as given.",
    _schema(
        {
            "person": {
                "type": "string",
                "description": "Whose receipts, when it isn't the signed-in person's own.",
            },
        }
    ),
    _catch_up_link,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)


def _set_receipt_day(t: Tools, args: dict) -> dict:
    return t.receipts.set_day(t, args)


SET_RECEIPT_DAY = ToolSpec(
    "set_receipt_day",
    "Say which day a receipt is from, for one sent later (a catch-up): find its line in "
    "month_report and pass its source as line, the day as YYYY-MM-DD, and the person's own "
    "words in said. It moves the receipt to that day's month, priced at that day's rate. "
    "Never pass a day they didn't say or agree to.",
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "day": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "said": {"type": "string", "description": "The person's own words about the day."},
        },
        ["line", "day", "said"],
    ),
    _set_receipt_day,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "Which day a catch-up receipt is from: line is its source from month_report, day as "
        "YYYY-MM-DD, said the person's own words. Never pass a day they didn't say or agree to."
    ),
)


def _receipts_to_read(t: Tools, args: dict) -> dict:
    return t.receipts.to_read(t, args)


RECEIPTS_TO_READ = ToolSpec(
    "receipts_to_read",
    "The next receipts waiting to be read (sent in a catch-up, with no day or amount yet), "
    "as images after this answer: the signed-in person's own, or pass person for someone "
    "whose month they review. Read each image and pass what it shows to record_reading. "
    "Each receipt's line is for your calls; never say it to the person.",
    _schema(
        {
            "person": {"type": "string", "description": "Whose receipts, when not their own."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
        }
    ),
    _receipts_to_read,
    short=(
        "The next catch-up receipts waiting to be read, as images after this answer: the person's"
        " own, or pass person for someone whose month they review. Pass what each image shows to "
        "record_reading."
    ),
)


def _record_reading(t: Tools, args: dict) -> dict:
    return t.receipts.record_reading(t, args)


RECORD_READING = ToolSpec(
    "record_reading",
    "Record what one receipt's image shows, after receipts_to_read: its line, the day "
    "(YYYY-MM-DD), the amount and currency code (USD, NPR...), who was paid (payee), and "
    "the category it reads as. Only what the receipt shows; leave a field out when it "
    "doesn't show it. This is a reading, never the person's word: the amount counts only "
    "once they agree to it (set_amount). The line is for your calls; never say it to the "
    "person.",
    _schema(
        {
            "line": {"type": "string", "description": "The receipt's line from receipts_to_read."},
            "day": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "amount": {"type": "string", "description": "The total paid, as printed."},
            "currency": {"type": "string", "description": "A currency code: USD, NPR, INR..."},
            "payee": {"type": "string", "description": "Who was paid, as printed."},
            "category": {"type": "string", "description": "What it reads as: Meals, Travel..."},
        },
        ["line", "day"],
    ),
    _record_reading,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "Record what one receipt's image shows, after receipts_to_read: line, day (YYYY-MM-DD), "
        "amount and currency code, payee, and category. Only what the receipt shows; leave out "
        "what it doesn't. A reading is never the person's word: the amount counts only once they "
        "agree to it (set_amount)."
    ),
)


def _agree_to_readings(t: Tools, args: dict) -> dict:
    return t.receipts.agree(t, args)


AGREE_TO_READINGS = ToolSpec(
    "agree_to_readings",
    "The person's one yes to every reading left in a month, after you've walked them "
    "through what was read (month_report's read on each line) and made their "
    "corrections with set_amount, set_receipt_day and set_category. Pass the month and "
    "their own words in said. Only on their yes; never on your own.",
    _schema(
        {
            "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
            "said": {"type": "string", "description": "The person's own words of agreement."},
            "person": {"type": "string", "description": "Whose month, when not their own."},
        },
        ["month", "said"],
    ),
    _agree_to_readings,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "The person's one yes to every reading left in a month, after you walked them through "
        "each reading and made their corrections (set_amount, set_receipt_day, set_category). "
        "Pass the month and their own words in said. Only on their yes."
    ),
)


def _send_month_for_review(t: Tools, args: dict) -> dict:
    return t.receipts.send_month(t, args)


SEND_MONTH_FOR_REVIEW = ToolSpec(
    "send_month_for_review",
    "Send a month to the reviewer as one card listing every receipt and drive, when the "
    "person says it's ready. Nothing goes while a line still waits on a day, an amount, "
    "or a rate; the answer says what.",
    _schema(
        {
            "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
            "person": {"type": "string", "description": "Whose month, when not their own."},
        },
        ["month"],
    ),
    _send_month_for_review,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
