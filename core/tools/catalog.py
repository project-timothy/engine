"""The engine's read-only tools: what a chat front end may ask the books.

Ask Tim, step 1 (the design note of 2026-10-04). Each tool answers one plain
question from the ledger: what is due, what is waiting for a yes, what is
still owed, did this morning's run work. Boundary classification
(docs/boundary-rules.md): **code**. A model may choose which tool to call and
explain the answer; the answer itself is computed here, every money value is
an exact decimal string, and every row names its source so it can be traced
to the books. Nothing here writes: the ledger is opened read-only and the run
lock is never taken, so a question mid-run neither blocks nor is blocked. The
one exception is ``decide_card``, offered only on the box's door with a
doorkeeper (``decider``), which writes through core/tools/decide.py and only
on the person's own Face ID.

With a ``viewer`` (core/tools/viewer.py), the tools answer as that person:
each row reaches them only when authority.toml lets them view its resource
in its unit, totals count only those rows, and a tool whose rows they could
never see is not offered. Without one, they answer for the whole tenant (the
owner's own local use).

Where authority.toml says ``log_reads = "always"`` (a sending agency, chunk
9), every call is written down before its answer goes back: who asked, which
tool, and whose rows reached them, in ``read_log.jsonl`` beside the ledger.
Never the rows. A separate append-only file, so a read still takes no lock and
waits on no run, and a read whose line cannot be written is not answered.

The tools themselves live beside this file, one module per domain
(2026-11-17, card C1): payables.py (deadlines, cards, invoices, runs,
expense reports), months.py (a person's month and year), books.py (the
books, board pack, CPA export, donor letters), tax.py (the tax packet,
payers, a church's pay), year.py (the year's close, budget, program
share), rules.py (the ministry's rules and the yes), start.py
(start_here), receipt_tools.py, and bank.py (statements and cash), over
spec.py (what a tool is) and reads.py (the shared reads). This file keeps
``Tools``, the tools every door offers (``SPECS``), and which of the rest
a person is offered; every name callers import from here still imports.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..agents.deadlines.schema import DEFAULT_LEAD_DAYS
from ..ledger.event_log import append_event_line
from .bank import BANK_STATEMENT as BANK_STATEMENT
from .bank import COUNT_CASH as COUNT_CASH
from .bank import EXPLAIN_BANK_LINE as EXPLAIN_BANK_LINE
from .bank import EXPLAIN_CASH as EXPLAIN_CASH
from .bank import KEEP_OWN_LINES as KEEP_OWN_LINES
from .bank import KEEP_SCHOOL_LIST as KEEP_SCHOOL_LIST
from .bank import PICK_CARD_RECEIPT as PICK_CARD_RECEIPT
from .bank import SPLIT_LINE as SPLIT_LINE
from .bank import STATEMENT_TOOLS
from .books import CPA_EXPORT, DONOR_LETTERS, LETTER_DETAILS, _board_pack, _books
from .months import _clock as _clock
from .months import _month_report, _year_report
from .months import _paid_from as _paid_from
from .months import _review_stands as _review_stands
from .months import cash_when as cash_when
from .payables import CLOSED_STATUSES as CLOSED_STATUSES
from .payables import _card_visible as _card_visible
from .payables import (
    _close_status,
    _deadlines,
    _expense_reports,
    _find_invoices,
    _open_payables,
    _recent_runs,
    _waiting_cards,
)
from .payables import open_deadlines as open_deadlines
from .reads import _events as _events
from .reads import _log as _log
from .reads import _sees_every_month
from .receipt_tools import (
    ADD_RECEIPT,
    AGREE_TO_READINGS,
    CATCH_UP_LINK,
    PAID_IT_MYSELF,
    RECEIPTS_TO_READ,
    RECORD_READING,
    SEND_MONTH_FOR_REVIEW,
    SET_AMOUNT,
    SET_CATEGORY,
    SET_DRIVE_PURPOSE,
    SET_RECEIPT_DAY,
)
from .rules import (
    ASK_FOR_OK,
    ASK_THE_RULES,
    DECIDE,
    DRAFT_RULE,
    HANDBOOK_CHANGES,
    _decides_rules,
    _rulebook,
    _watched,
)
from .spec import _LIMIT, ADDED, ToolSpec, _schema

# Re-exported: callers and tests import these from the catalog.
from .spec import register as register
from .start import _next_and_first, _start_here
from .tax import KEEP_PAYERS, SET_PAY, TAX_PACKET, _has_a_packet, _sets_pay
from .viewer import Viewer
from .year import CLOSE_YEAR, REOPEN_YEAR, SET_BUDGET, SET_PROGRAM_SHARE, _closed_refusal

READ_LOG = "read_log.jsonl"
"""The door's read log, one JSON line per call, beside the ledger."""


MINISTRY_UNIT = "ministry"
"""How the read log names a row that is in no one's unit (the ministry's own
bill, the books)."""


@dataclass
class Tools:
    tenant: str
    ledger_root: Path
    obligations_file: Path | None = None
    today: str | None = None
    lead_days: tuple[int, ...] = DEFAULT_LEAD_DAYS
    viewer: Viewer | None = None
    decider: Any = None  # core/tools/decide.Decider, on the box's door only
    receipts: Any = None  # core/tools/receipts.Receipts, on the box's door only
    ministry: str = ""  # the ministry's name, said back by start_here
    shown: Any = None  # core/tools/first_time.Shown, on the box's door only
    talk_page: str = ""  # Tim's own page, where his voice is on (card T2); start_here names it
    _specs: dict[str, ToolSpec] = field(default_factory=dict, init=False, repr=False)
    _reached: set[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._specs = {
            spec.name: spec
            for spec in SPECS
            if (spec.name not in BOOKS_TOOLS or self.sees("books"))
            and (spec.name not in MONEY_TOOLS or self._sees_anywhere(MONEY_TOOLS[spec.name]))
        }
        if self.decider is not None and self.decider.offered():
            self._specs[DECIDE.name] = DECIDE
        book = _rulebook(self)
        if book is not None:  # only a ministry that wrote rules down
            self._specs[ASK_THE_RULES.name] = ASK_THE_RULES
            # The box's door, and a rule that asks for an OK first (a plan
            # alone asks for none).
            if book.asks_first and self.receipts is not None and self.receipts.offered():
                self._specs[ASK_FOR_OK.name] = ASK_FOR_OK
            # The box's door, a handbook copy the watch reads, and someone
            # who decides the ministry's rules (t-rule-drafting).
            if self.receipts is not None and _watched(book) and _decides_rules(self):
                self._specs[HANDBOOK_CHANGES.name] = HANDBOOK_CHANGES
                self._specs[DRAFT_RULE.name] = DRAFT_RULE
        if self.receipts is not None and _sets_pay(self):  # a church's door, c34
            self._specs[SET_PAY.name] = SET_PAY
        if self.receipts is not None and self.receipts.offered():
            self._specs[ADD_RECEIPT.name] = ADD_RECEIPT
        if self.receipts is not None and _sees_every_month(self):  # the box's door
            self._specs[SET_BUDGET.name] = SET_BUDGET
            self._specs[CLOSE_YEAR.name] = CLOSE_YEAR
            self._specs[REOPEN_YEAR.name] = REOPEN_YEAR
            self._specs[SET_PROGRAM_SHARE.name] = SET_PROGRAM_SHARE
        if self.viewer is not None and _sees_every_month(self):  # read only: any door
            self._specs[CPA_EXPORT.name] = CPA_EXPORT
            self._specs[DONOR_LETTERS.name] = DONOR_LETTERS
        if self.receipts is not None and self.viewer is not None and _sees_every_month(self):
            self._specs[LETTER_DETAILS.name] = LETTER_DETAILS  # the box's door
        if self.receipts is not None and self.viewer is not None:  # the box's door, anyone
            self._specs[PAID_IT_MYSELF.name] = PAID_IT_MYSELF
        if self.viewer is not None and _has_a_packet(self):  # a missionary's, c33
            self._specs[TAX_PACKET.name] = TAX_PACKET
            if self.receipts is not None:  # the box's door
                self._specs[KEEP_PAYERS.name] = KEEP_PAYERS
        if self.receipts is not None and self.receipts.sorts(self.viewer):
            self._specs[SET_CATEGORY.name] = SET_CATEGORY
            self._specs[SET_AMOUNT.name] = SET_AMOUNT
            self._specs[SET_DRIVE_PURPOSE.name] = SET_DRIVE_PURPOSE
            self._specs[CATCH_UP_LINK.name] = CATCH_UP_LINK
            self._specs[SET_RECEIPT_DAY.name] = SET_RECEIPT_DAY
            self._specs[RECEIPTS_TO_READ.name] = RECEIPTS_TO_READ
            self._specs[RECORD_READING.name] = RECORD_READING
            self._specs[AGREE_TO_READINGS.name] = AGREE_TO_READINGS
            self._specs[SEND_MONTH_FOR_REVIEW.name] = SEND_MONTH_FOR_REVIEW
            for spec in STATEMENT_TOOLS:
                self._specs[spec.name] = spec
        for spec, offered in ADDED:
            if offered(self):
                self._specs[spec.name] = spec

    # -- plumbing --------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def call(self, name: str, args: dict | None = None) -> dict:
        logs = self.viewer is not None and self.viewer.policy.safeguards.log_reads == "always"
        if name not in self._specs:
            if logs:
                self._log_read(name, set(), refused=True)
            raise KeyError(f"no tool {name!r}; tools: {', '.join(self._specs)}")
        closed = _closed_refusal(self, name, dict(args or {}))
        if closed is not None:
            return closed
        if not logs:
            return self._ends_with_next(name, self._specs[name].fn(self, dict(args or {})))
        self._reached = set()
        answered = False
        try:
            answer = self._ends_with_next(name, self._specs[name].fn(self, dict(args or {})))
            answered = True
        finally:
            reached, self._reached = self._reached or set(), None
            # A call the tool refused (it raised) is on record too, as refused.
            self._log_read(name, reached, refused=not answered)
        return answer

    def _ends_with_next(self, name: str, answer: Any) -> Any:
        """Every answer through a person's door ends with the one next thing
        waiting on them (docs/screens/README.md, chunk 1); start_here says it
        first. Just before it, the first time they meet something new, what
        it is, why, and the rule (chunk 2, core/tools/first_time.py). The
        owner's own use (no viewer) answers as before."""
        if self.viewer is None or name == "start_here" or not isinstance(answer, dict):
            return answer
        try:
            nxt, first = _next_and_first(self, explain="first_time" not in answer)
        except Exception:  # noqa: BLE001  a broken next thing never costs the person their answer
            return answer
        out = {k: v for k, v in answer.items() if k != "next"}
        if first is not None:
            out["first_time"] = first
        return {**out, "next": nxt}

    def _log_read(self, tool: str, reached: set[str], *, refused: bool) -> None:
        """One line in the read log; raises when it cannot be written, so the
        answer never goes back unrecorded."""
        if self.viewer is None:
            return  # the owner's own local use answers as nobody
        record = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "tenant": self.tenant,
            "person": self.viewer.person,
            "tool": tool,
            "reached": sorted(reached),
            "refused": refused,
        }
        append_event_line(Path(self.ledger_root), record, filename=READ_LOG)

    def _db(self) -> sqlite3.Connection:
        path = Path(self.ledger_root) / "ledger.sqlite3"
        if not path.is_file():
            raise FileNotFoundError(f"no ledger for {self.tenant} at {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        con = self._db()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def _today(self) -> date:
        return date.fromisoformat(self.today) if self.today else date.today()

    def sees(self, resource: str, unit: str = "") -> bool:
        """Whether the person asking may view this row; always, with no viewer.
        While a call is being logged, a row they may view counts as reaching
        them (the log errs toward more, never less)."""
        allowed = self.viewer is None or self.viewer.sees(resource, unit)
        if allowed and self._reached is not None:
            self._reached.add(unit or MINISTRY_UNIT)
        return allowed

    def _sees_anywhere(self, resource: str) -> bool:
        """Whether any row of ``resource`` could ever reach this person: in
        the ministry's own books or in some person's unit."""
        if self.viewer is None:
            return True
        units = ["", *self.viewer.policy.people]
        return any(self.viewer.sees(resource, unit) for unit in units)

    def _cap(self, limit: int) -> int:
        """The SQL LIMIT: the caller's, or none when rows are filtered after
        the query (a limit before the filter could hide what the person may
        see behind rows they may not)."""
        return limit if self.viewer is None else -1


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "start_here",
        "The one next thing waiting on the person (next: what to say, what it takes, and the "
        "tool and args that do it, worked out from their books; lead with it in your own "
        "words, and when they agree, call that tool), then who the signed-in person is "
        "(their name, what they do, the ministry, whose spending "
        "they look over and who looks over theirs, and the people they may ask about, for "
        "member care the people in their care) and Tim's own answers to how-do-I "
        "questions, for the tools this person has: answers, the ones for everyone in full, and "
        "more, each other question by id; to answer one of those, call start_here again with "
        "its id in about. Call it at the start of a new chat, when "
        "the person greets Tim (or Timothy or Timmy) or asks what to do first, and whenever "
        "they ask who they are, how to do something with Tim, or what Tim can do, and answer "
        "from it in your own words. The first answer is the one setting "
        "that lets Tim work without asking each time: offer it to anyone whose app keeps "
        "asking. Every other answer ends with next too: close your reply with it.",
        _schema({"about": {"type": "array", "items": {"type": "string"}, "maxItems": 10}}),
        _start_here,
        short=(
            "The one next thing waiting on the person (next: lead with it in your own words; when"
            " they agree, call its tool with its args), who they are (their name, what they do, "
            "the ministry, whose spending they look over and who looks over theirs, the people "
            "they may ask about), and Tim's answers to how-do-I questions for their tools: some "
            "in full, the rest by id (to answer one, call again with its id in about). Call it at"
            " the start of a conversation, when they greet Tim or ask what to do first, and when "
            "they ask who they are, how to do something, or what Tim can do."
        ),
    ),
    ToolSpec(
        "deadlines",
        "Dated obligations (filings, renewals, reports) due within the next N days, "
        "from the tenant's obligations file, minus any marked done. A Form 990 row also says "
        "the tax year it covers, what came into the ministry's account that year "
        "(gross_receipts), what to file, what the form asks for (asks_for), and what Tim "
        "already knows (known): tell the person plainly and quote the figure as given.",
        _schema({"days": {"type": "integer", "minimum": 0, "maximum": 3650}}),
        _deadlines,
        short=(
            "Dated obligations (filings, renewals, reports) due within the next days, minus any "
            "marked done. A Form 990 row also says the tax year it covers, gross_receipts, what "
            "to file, asks_for, and known (what Tim already knows): tell the person plainly."
        ),
    ),
    ToolSpec(
        "waiting_cards",
        "Approval cards still waiting for a yes or no. Speak of each by its about line: "
        "whose it is, what it's for, and how much. The card number is only for decide_card, "
        "never for the person.",
        _schema(_LIMIT),
        _waiting_cards,
        short=(
            "Cards waiting for a yes or no; yours says if this person decides it: ask here "
            "who approves. Say each by its about line. The card number is for decide_card."
        ),
    ),
    ToolSpec(
        "open_payables",
        "Vendor invoices not yet paid, earliest due first, with the count, the total owed, "
        "and a subtotal per vendor, all over every open bill even when the list is cut "
        "short. Quote those sums as given rather than adding amounts yourself. Each row's "
        "source is for your calls, never for the person's ears.",
        _schema(_LIMIT),
        _open_payables,
        short=(
            "Vendor invoices not yet paid, earliest due first, with the count, the total owed, "
            "and a subtotal per vendor, all over every open bill even when the list is cut short."
        ),
    ),
    ToolSpec(
        "find_invoices",
        "Find vendor invoices by vendor name or invoice number (any status), with the count, "
        "the total, and a subtotal per status (paid, unpaid, scheduled), all over every match "
        "even when the list is cut short. Quote those sums as given rather than adding "
        "amounts yourself. Each row's source is for your calls, "
        "never for the person's ears.",
        _schema({"query": {"type": "string"}, **_LIMIT}, ["query"]),
        _find_invoices,
        short=(
            "Find vendor invoices by vendor name or invoice number (any status), with the count, "
            "the total, and a subtotal per status, all over every match even when the list is cut"
            " short."
        ),
    ),
    ToolSpec(
        "recent_runs",
        "The latest run of every scheduled job and how it ended: did this morning's run work.",
        _schema(_LIMIT),
        _recent_runs,
    ),
    ToolSpec(
        "close_status",
        "Month-end close progress: the last month sealed and the latest close events.",
        _schema(_LIMIT),
        _close_status,
    ),
    ToolSpec(
        "expense_reports",
        "Expense reports with their totals and where each stands: waiting for approval, "
        "approved (going into the books on the next run), open, reimbursed, or cleared. To "
        "approve or decline one, find it in waiting_cards and use decide_card.",
        _schema(_LIMIT),
        _expense_reports,
    ),
    ToolSpec(
        "month_report",
        "A person's month of receipts, for the person or the one who reviews them: each receipt's "
        "date and dollars, a receipt paid in another currency shown as the person said it beside "
        "its dollar value at the exchange rate on the receipt's own date, what the month spent "
        "(spent: every dollar, whoever paid), what is owed back to the person (total: only what "
        "they paid themselves; a line paid from the ministry's cash and theirs says its owed_back "
        "part), and where the review stands. Quote amounts and rates as given, "
        "never converting yourself; a receipt still waiting on its day's rate has no dollar value "
        "yet, so say so. Leave month out for this month (YYYY-MM otherwise); person narrows it to "
        "one name. To approve the month, find its card in waiting_cards. It also lists the "
        "person's drives (when, how far, and what each was for): when they ask about their drives,"
        " read them here and ask about each one still waiting on their word. Its bank lines are "
        "the month's statement lines and school payments: what each was (owed back, the ministry "
        "paid, sent abroad, paid back, came in) and what each still waits on; a line waiting on "
        "which receipt it paid lists could_be (ask, then pick_card_receipt). A line told in words "
        "carries told_by and told_at (who told Tim, and when, in UTC): when they ask where a line "
        "came from, say who told Tim and when. rules_say, when "
        "present, is what the ministry's own rules say about the month, each with the document and"
        " page it came from: tell the person plainly, as their ministry's rule, never as your "
        "judgment. Each line's source is for your calls; never say it to the person.",
        _schema(
            {
                "person": {"type": "string"},
                "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
            }
        ),
        _month_report,
        short=(
            "A person's month (month YYYY-MM, left out for this month; person narrows it to one "
            "name, for whoever reviews them): each receipt's day and dollars (one paid in another"
            " currency shown as they said it beside its dollar value at that day's rate), spent "
            "(every dollar, whoever paid), total (what is owed back to them: only what they paid "
            "themselves; a line paid partly from the ministry's cash says its owed_back part), "
            "and where the review stands. A receipt still waiting on its day's rate has no dollar"
            " value yet: say so. To approve the month, find its card in waiting_cards. drives: "
            "when they ask about their drives, ask about each one still waiting on their word. "
            "Bank lines: what each was and what it still waits on; a line with could_be: ask "
            "which receipt it paid, then pick_card_receipt. told_by and told_at say who told Tim "
            "about a line and when (UTC). rules_say: what the ministry's own rules say about the "
            "month, with document and page; tell it as their ministry's rule, never as your "
            "judgment."
        ),
    ),
    ToolSpec(
        "year_report",
        "Where a person's year stands, for the person or the one who reviews them: each "
        "month's receipts, what it spent (spent: every dollar of ministry spending, whoever "
        "paid), what is owed back to the person (owed_back: what they paid themselves; the "
        "ministry's own cash or card is never owed back), how many still wait on a day, an "
        "amount or a rate, "
        "and where its review stands; totals by category; and receipts sent in a catch-up "
        "that aren't in a month yet; and, once the ministry has set a budget for the year, "
        "each kind's budget, spent and left (say its says line). Quote it as given. Leave "
        "year out for this year.",
        _schema(
            {
                "person": {"type": "string"},
                "year": {"type": "string", "pattern": "^\\d{4}$"},
            }
        ),
        _year_report,
        short=(
            "Where a person's year stands (year left out for this year; person narrows it to one "
            "name): each month's receipts, spent (every dollar of ministry spending, whoever "
            "paid), owed_back (what they paid themselves; the ministry's own cash or card is "
            "never owed back), how many wait on a day, an amount, or a rate, and each month's "
            "review; totals by category; catch-up receipts not in a month yet; and each kind's "
            "budget, spent, and left (say its says line)."
        ),
    ),
    ToolSpec(
        "books",
        "The ministry's books for any span, asked by anyone at any time: a month, a quarter, "
        "a year, or year to date (leave period out for year to date). What came in and what "
        "was spent, each by kind; came in less spent; each month of the span; what is owed "
        "back to people and what was paid back; what is owed to the ministry, net of a "
        "person's own money that came in (held_for_people when their own money is more); "
        "how many lines "
        "still wait on something; cash in hand at the end; what is in the ministry's bank at the "
        "end (in_the_bank, once a statement carried the bank's balance: when agrees is false, "
        "say its say line); and, once a budget is set for the "
        "year, each budget as of the span's end (say its says line). It covers only the people "
        "the asker may see: for says whose books these are. Money sent abroad or drawn as cash "
        "counts as spent when its receipts are spent, never twice. double_entry is the same "
        "books as balanced entries: whether they balance, whether the bank account in them "
        "matches the bank (ties_to_the_bank), and each account at the span's end; say it "
        "only when asked whether the books balance or for the accounts. Quote the dollars as "
        "given; start with the says line.",
        _schema(
            {
                "period": {
                    "type": "string",
                    "description": "2026-09, September, 2026-Q3, last quarter, 2025, "
                    "last year, YTD",
                },
                "person": {"type": "string"},
            }
        ),
        _books,
        short=(
            "The ministry's books for any span (period: a month, quarter, or year; left out for "
            "year to date): what came in and was spent, by kind and by month; what is owed back "
            "to people and what was paid back; what is owed to the ministry (held_for_people when"
            " a person's own money is more); lines still waiting; cash in hand; in_the_bank (when"
            " agrees is false, say its say line); and each budget (say its says line). It covers "
            "only the people the asker may see: for says whose books these are. Money sent abroad"
            " or drawn as cash counts as spent once, when its receipts are spent. double_entry: "
            "say it only when asked whether the books balance or for the accounts. Start with the"
            " says line."
        ),
    ),
    ToolSpec(
        "board_pack",
        "The ministry's statements for any span, the way a board reads them: the statement "
        "of activities (what came in and what was spent, by kind, and the change in net "
        "assets), the statement of financial position at the span's end (the bank, cash in "
        "hand, ministry money people hold, what the ministry owes people, net assets), the "
        "program and management split once the ministry set one, each month, and the budget. "
        "Every figure is the books' own. Start with the says line; quote the dollars as given.",
        _schema(
            {
                "period": {
                    "type": "string",
                    "description": "2026-09, September, 2026-Q3, last quarter, 2025, "
                    "last year, YTD",
                },
                "person": {"type": "string"},
            }
        ),
        _board_pack,
        short=(
            "The ministry's statements for any span, the way a board reads them: the statement of"
            " activities (came in, spent by kind, the change in net assets), the statement of "
            "financial position at the span's end, the program and management split once the "
            "ministry set one, each month, and the budget. Start with the says line."
        ),
    ),
)


TOOL_NAMES: tuple[str, ...] = tuple(s.name for s in SPECS)


MONEY_TOOLS = {
    "open_payables": "ap.invoice",
    "find_invoices": "ap.invoice",
    "expense_reports": "expense.report",
    "month_report": "expense.report",
    "year_report": "expense.report",
    "books": "expense.report",
    "board_pack": "expense.report",
}
"""Tools whose every row is money, by the resource each row is: offered only
to a person some row of it could reach (member care sees people and their
dates, so an empty answer never reads to them as "Mei has no expenses")."""


BOOKS_TOOLS = frozenset({"recent_runs", "close_status"})
"""Tools about the books as a whole (the runs, the month's lock): offered
only to a person who may view the books in every unit."""


# Tools in their own modules register themselves on import (plan rule 9).
from . import first_time as _first_time  # noqa: E402, F401
from . import tax_facts as _tax_facts  # noqa: E402, F401
