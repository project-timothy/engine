"""The catch-up's bank statements, school list, and cash (t-statements):
each answers through core/tools/receipts.py. Moved out of
core/tools/catalog.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .spec import ToolSpec, _schema

if TYPE_CHECKING:
    from .catalog import Tools


def _bank_statement(t: Tools, args: dict) -> dict:
    return t.receipts.bank_statement(t, args)


BANK_STATEMENT = ToolSpec(
    "bank_statement",
    "Bank statements sent through a catch-up link (the CSV a bank's website exports). With "
    "nothing named: what waits for the person's word. With a statement and its account: "
    "ministry keeps every line of the ministry's own account; own shows the lines of the "
    "person's own account, each with what Tim thinks it looks like, for them to pick with "
    "keep_own_lines (only they may see their own account). Ask whose account it is; never "
    "guess. When the statements' file names say whose each is, this tool gives the one "
    "question to ask; on their yes, as_named true settles every one as its name says. A "
    "payment service's export (PayPal, a church giving app) is kept only as the ministry's "
    "own account; when its service isn't named, ask which and pass it as service. Ids and "
    "row numbers are for your calls; never say them to the person.",
    _schema(
        {
            "statement": {"type": "string", "description": "The statement's id, from this tool."},
            "account": {"type": "string", "enum": ["ministry", "own"]},
            "person": {"type": "string", "description": "Whose, when not their own."},
            "service": {
                "type": "string",
                "description": "A payment service export's service, as they name it, when "
                "Tim couldn't tell (Zeffy, Planning Center).",
            },
            "as_named": {
                "type": "boolean",
                "description": "Only on their yes to the one question the names raised.",
            },
        }
    ),
    _bank_statement,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "Bank statements from a catch-up link. Nothing named: what waits on their word. A "
        "statement and account: ministry keeps every line; own shows their own lines to"
        " pick with keep_own_lines. Ask whose it is, never guess; when the file names say, ask "
        "the one question given, and their yes is as_named true. A payment service's export is"
        " kept only as the ministry's; if its service isn't named, ask and pass service."
    ),
)


def _keep_own_lines(t: Tools, args: dict) -> dict:
    return t.receipts.keep_own(t, args)


KEEP_OWN_LINES = ToolSpec(
    "keep_own_lines",
    "Keep the lines the person picked from their own account's statement, after "
    "bank_statement showed them: pass the statement, the rows they picked, and their own "
    "words in said. When they say to keep what Tim flagged, pass flagged true, with the "
    "rows of any others they name. Only the picked lines are kept; the rest are gone from "
    "Tim. Never pick for them.",
    _schema(
        {
            "statement": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            "flagged": {
                "type": "boolean",
                "description": "When they said keep what Tim flagged.",
            },
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["statement", "said"],
    ),
    _keep_own_lines,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        "Keep the lines the person picked from their own statement: rows, flagged true if "
        "they said keep what Tim flagged, and their words. The rest are gone. Never pick for "
        "them."
    ),
)


def _keep_school_list(t: Tools, args: dict) -> dict:
    return t.receipts.keep_school(t, args)


KEEP_SCHOOL_LIST = ToolSpec(
    "keep_school_list",
    "Add the person's school list (scholarships and school fees, sent through a catch-up "
    "link): each payment becomes a line in its own month with no receipt needed. Pass the "
    "statement id bank_statement listed for it, and their words in said.",
    _schema(
        {
            "statement": {"type": "string"},
            "said": {"type": "string"},
            "person": {"type": "string", "description": "Whose, when not their own."},
        },
        ["statement"],
    ),
    _keep_school_list,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)


def _explain_bank_line(t: Tools, args: dict) -> dict:
    return t.receipts.explain_line(t, args)


EXPLAIN_BANK_LINE = ToolSpec(
    "explain_bank_line",
    "Say what a bank line was for, in the person's own words, when it has no receipt "
    "(month_report's bank lines say which wait). Pass its source as line and their words in "
    "said. A receipt sent for it later finds its line by itself. For cash from an ATM, their "
    "words say how many rupees (or whichever money) it gave, from the slip. Tim names the "
    "category; when it has none, propose one from the ministry's list and pass it back as "
    "category once they agree. A line on the ministry's account they bought for themselves "
    "is whose own: owed to the ministry, never its spending. Money in on the ministry's "
    "account that was theirs (their pay from a support platform, a gift given for them) is "
    "whose own too: it goes to their fund, never the ministry's income. A deposit with names "
    "names a person: ask whether it was that person's pay. Money in that was a gift for "
    "them (a birthday gift from their mom) is whose own with gift_from naming the giver: a "
    "gift on that one line, kept off their taxes. A line on the ministry's "
    "account that paid someone back for an expense they paid themselves (paid_it_myself) "
    "names them in pays_back. A gift to the ministry names who gave it in gift_from (ask "
    "when it isn't clear); a grant from a donor-advised fund also sets fund_grant. A gift to "
    "the ministry given toward a missionary it supports also names them in for_fund: it stays "
    "the ministry's income and the donor's gift, and counts toward that missionary's fund. "
    "Family can't give toward a missionary's fund; Tim refuses it and says why. A "
    "ministry cost on their own account that they paid from their own pay, with no one "
    "paying them back, is whose pay: it comes off their 1099 income on their tax packet. "
    "Only on their word; a sending church often pays costs back. A payment out of the "
    "ministry's account that was a missionary's pay under the board's resolution names them "
    "in pay_of.",
    _schema(
        {
            "line": {"type": "string", "description": "The bank line's source from month_report."},
            "said": {"type": "string", "description": "The person's own words."},
            "category": {
                "type": "string",
                "description": "One of the ministry's categories, once they agree to it.",
            },
            "whose": {
                "type": "string",
                "enum": ["ministry", "own", "pay"],
                "description": "own: the ministry's money spent on themselves, or money in "
                "that was theirs (their fund). pay: a ministry cost on their own account they "
                "paid from their own pay, with no one paying them back.",
            },
            "pays_back": {
                "type": "string",
                "description": "The full name of the person this line paid back.",
            },
            "pay_of": {
                "type": "string",
                "description": "The full name of the person whose pay this line was.",
            },
            "gift_from": {
                "type": "string",
                "description": "Who gave this money in, as they'd sign it (a person, a "
                "family, a church, or a fund): to the ministry, or with whose own, to them.",
            },
            "fund_grant": {
                "type": "boolean",
                "description": "True when the gift is a grant from a donor-advised fund.",
            },
            "for_fund": {
                "type": "string",
                "description": "The full name of the missionary whose fund a gift to the "
                "ministry was given toward.",
            },
            "not_family": {
                "type": "boolean",
                "description": "True once the person says a giver who shares the missionary's "
                "last name isn't their family.",
            },
        },
        ["line", "said"],
    ),
    _explain_bank_line,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    short=(
        "Say what a bank line was for, receipt or not: line is its source (from month_report's "
        "bank lines), said is the person's words (for cash from an ATM, how much it gave, from "
        "the slip). Tim names the category; with none, propose one from the ministry's list and "
        "pass it as category once they agree. whose own: on the ministry's account or card, "
        "something they bought for themselves (owed to the ministry), or money in that was "
        "theirs (their pay from a support platform, a gift for them), which goes to their fund, "
        "never the ministry's income. A deposit with names: ask whether it was that person's "
        "pay. A gift for them: whose own with gift_from naming the giver (kept off their "
        "taxes). pays_back: who this line paid back for something they paid themselves "
        "(paid_it_myself). A gift to the ministry: gift_from names the giver (ask when "
        "unclear); fund_grant for a donor-advised fund's grant; for_fund names the missionary "
        "it was given toward (still the ministry's income; family can't give toward a "
        "missionary's fund, and Tim says why). whose pay: a ministry cost on their own account "
        "paid from their own pay, never paid back; only on their word, since a sending church "
        "often pays costs back. pay_of: a payment out that was a missionary's pay under the "
        "board's resolution."
    ),
)


def _split_line(t: Tools, args: dict) -> dict:
    return t.receipts.split_line(t, args)


SPLIT_LINE = ToolSpec(
    "split_line",
    "Take an amount out of one line when the person says part of it was something else "
    '("1,300 of that was mine", "5,000 of that was scholarships"): a bank line, cash with no '
    "receipt, or a receipt (its source, or a bank line's receipt_source). The rest stays "
    "where it was. Pass the line's source from month_report as line, their words in said "
    "(the amount in the line's money, never a percent), and whose: own when the part was "
    "theirs, else the part's category. Never ask for a split; take one only when they say "
    "it. Saying a part of the same kind again replaces it. The parts add up to the line to "
    "the cent.",
    _schema(
        {
            "line": {"type": "string", "description": "The line's source from month_report."},
            "said": {"type": "string", "description": "The person's own words."},
            "whose": {
                "type": "string",
                "enum": ["ministry", "own"],
                "description": "own when the part was theirs; ministry (the default) otherwise.",
            },
            "category": {
                "type": "string",
                "description": "The ministry category of a ministry part.",
            },
        },
        ["line", "said"],
    ),
    _split_line,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    short=(
        'Take part of one line out when the person says part of it was something else ("1,300 of '
        'that was mine"): a bank line, cash with no receipt, or a receipt. line: its source from '
        "month_report, or a bank line's receipt_source for rupees off a dollar charge (never ask "
        "for dollars). said: their words, an amount, not a percent. whose: own when theirs, "
        "else the part's category. Only when they say it. A same-kind part again replaces it."
    ),
)


def _count_cash(t: Tools, args: dict) -> dict:
    return t.receipts.count_cash(t, args)


COUNT_CASH = ToolSpec(
    "count_cash",
    'When the person says how much cash they have in hand ("I\'ve got 9,000 rupees left"), '
    "pass their words in said. Tim holds it against the cash the books say they hold and "
    "says what to ask next: what any missing cash went to, with no receipt. Ask for a count "
    "once a month, or when Tim's morning email asks.",
    _schema(
        {"said": {"type": "string", "description": "The person's own words: the cash in hand."}},
        ["said"],
    ),
    _count_cash,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    short=(
        'When the person says how much cash they have in hand ("I\'ve got 9,000 rupees left"), '
        "pass their words in said. Tim holds it against the cash the books say they hold and says"
        " what to ask next. Ask for a count once a month."
    ),
)


def _explain_cash(t: Tools, args: dict) -> dict:
    return t.receipts.explain_cash(t, args)


EXPLAIN_CASH = ToolSpec(
    "explain_cash",
    "After count_cash finds cash missing, say where it went, in the person's own words: "
    'ministry spending with no receipt ("tea, taxis and tips") or their own use. whose is '
    "ministry or own, from what they said. An amount in their words takes that much; none "
    "takes all that's still missing. Call once for each part when they say some of each.",
    _schema(
        {
            "said": {"type": "string", "description": "The person's own words."},
            "whose": {
                "type": "string",
                "enum": ["ministry", "own"],
                "description": "ministry: spent on the ministry; own: their own use.",
            },
            "category": {
                "type": "string",
                "description": "One of the ministry's categories, once they agree to it.",
            },
        },
        ["said", "whose"],
    ),
    _explain_cash,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    short=(
        "After count_cash finds cash missing, say where it went in the person's own words: whose "
        "ministry (spending with no receipt) or own. An amount in their words takes that much; "
        "none takes all that's still missing. One call for each part when they say some of each."
    ),
)


def _pick_card_receipt(t: Tools, args: dict) -> dict:
    return t.receipts.pick_card_receipt(t, args)


PICK_CARD_RECEIPT = ToolSpec(
    "pick_card_receipt",
    "When a bank line in month_report waits on which receipt it paid, its could_be lists the "
    "person's receipts that could each be it: ask which one they paid by card, then pass the "
    "line's source as line, that receipt's source as receipt, and their words in said. The "
    "other receipts stay receipts of their own (cash, or paid some other way).",
    _schema(
        {
            "line": {"type": "string", "description": "The bank line's source from month_report."},
            "receipt": {"type": "string", "description": "The source of the receipt they named."},
            "said": {"type": "string", "description": "The person's own words."},
        },
        ["line", "receipt", "said"],
    ),
    _pick_card_receipt,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    short=(
        "When a month_report bank line lists could_be, ask which of those receipts they paid by "
        "card, then pass the line's source as line, that receipt's source as receipt, and their "
        "words in said. The others stay receipts of their own."
    ),
)


STATEMENT_TOOLS = (
    BANK_STATEMENT,
    KEEP_OWN_LINES,
    KEEP_SCHOOL_LIST,
    EXPLAIN_BANK_LINE,
    SPLIT_LINE,
    COUNT_CASH,
    EXPLAIN_CASH,
    PICK_CARD_RECEIPT,
)
"""The catch-up's bank statements and school list (t-statements)."""
