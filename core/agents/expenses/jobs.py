"""Expenses agent jobs: the registry the engine loads (JOBS, CARD_AUTHORITY).

Each job lives in its own module (job_intake, job_extract, job_report,
job_send, job_edits, job_match, job_inbox, job_lines) over the shared
helpers in common, report_lines, and confirm_card.

Design of record: ``docs/expenses-design.md`` (approved 2026-08-04). The spine
is deterministic; the LLM's only role is per-receipt field proposals that
land on a review card (invariant 2). Every QBO write is approval-gated,
provenance-stamped, duplicate-guarded, and readback-verified (the W1
contract). The engine never moves money (invariant 7): the owner reimburses,
The expenses agent records.
"""

from __future__ import annotations

from ...authority import CardRule
from ...engine.contracts import JobHandler
from . import advances, board_pack, donors, payers, tax_facts, year_close
from .confirm_card import CONFIRM_CARD
from .job_edits import (
    _amount_key,
    _amount_run,
    _date_key,
    _date_run,
    _drive_key,
    _drive_run,
    _purpose_key,
    _purpose_run,
)
from .job_extract import _extract_key, _extract_run
from .job_inbox import INBOX_FILE_CARD, INBOX_SKIP_CARD, _inbox_key, _inbox_run
from .job_intake import _intake_key, _intake_run
from .job_lines import (
    _budget_key,
    _budget_run,
    _cash_key,
    _cash_run,
    _line_purpose_key,
    _line_purpose_run,
    _line_split_key,
    _line_split_run,
    _processor_key,
    _processor_run,
    _school_key,
    _school_run,
    _statement_key,
    _statement_run,
)
from .job_match import _match_key, _match_run
from .job_report import (
    DRAFT_CARD,
    _categorize_key,
    _categorize_run,
    _month_key,
    _month_run,
    _report_key,
    _report_run,
)
from .job_send import _send_key, _send_run
from .records import (
    REVIEW_CARD,
    SEND_CARD,
)

JOBS: dict[str, JobHandler] = {
    "inbox": JobHandler(key=_inbox_key, run=_inbox_run),
    "intake": JobHandler(key=_intake_key, run=_intake_run),
    "extract": JobHandler(key=_extract_key, run=_extract_run),
    "report": JobHandler(key=_report_key, run=_report_run),
    "month": JobHandler(key=_month_key, run=_month_run),
    "date": JobHandler(key=_date_key, run=_date_run),
    "categorize": JobHandler(key=_categorize_key, run=_categorize_run),
    "drive": JobHandler(key=_drive_key, run=_drive_run),
    "drive_purpose": JobHandler(key=_purpose_key, run=_purpose_run),
    "amount": JobHandler(key=_amount_key, run=_amount_run),
    "statement": JobHandler(key=_statement_key, run=_statement_run),
    "processor": JobHandler(key=_processor_key, run=_processor_run),
    "school": JobHandler(key=_school_key, run=_school_run),
    "line_purpose": JobHandler(key=_line_purpose_key, run=_line_purpose_run),
    "line_split": JobHandler(key=_line_split_key, run=_line_split_run),
    "cash": JobHandler(key=_cash_key, run=_cash_run),
    "budget": JobHandler(key=_budget_key, run=_budget_run),
    "close_year": JobHandler(key=year_close.key, run=year_close.run),
    "reopen_year": JobHandler(key=year_close.reopen_key, run=year_close.reopen),
    "advance": JobHandler(key=advances.key, run=advances.run),
    "program_share": JobHandler(key=board_pack.split_key, run=board_pack.split_run),
    "letter_detail": JobHandler(key=donors.detail_key, run=donors.detail_run),
    "payer": JobHandler(key=payers.payer_key, run=payers.payer_run),
    "payer_form": JobHandler(key=payers.form_key, run=payers.form_run),
    "tax_fact": JobHandler(key=tax_facts.fact_key, run=tax_facts.fact_run),
    "tax_travel": JobHandler(key=tax_facts.travel_key, run=tax_facts.travel_run),
    "send": JobHandler(key=_send_key, run=_send_run),
    "match": JobHandler(key=_match_key, run=_match_run),
}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {
    INBOX_FILE_CARD: CardRule("approve", "expense.report", money=False),
    INBOX_SKIP_CARD: CardRule("approve", "expense.report", money=False),
    # A month's review: ``#month`` names it in a grant, so a solo ministry's
    # month approver decides it and nothing else (board c48, 2026-11-17).
    REVIEW_CARD: CardRule(
        "approve",
        "expense.report",
        amount="total_cents",
        cents=True,
        submitter="person",
        kind="month",
    ),
    DRAFT_CARD: CardRule("approve", "expense.report", submitter="person"),
    # Sending claims commits none of the ministry's money: the institution
    # approves and pays in its own system. The person decides their own.
    SEND_CARD: CardRule("submit", "expense.report", money=False, submitter="person"),
    CONFIRM_CARD: CardRule(
        "approve", "payment", amount="total_cents", cents=True, submitter="person"
    ),
    # The year's close is decided as its months are: by who approves them.
    year_close.CLOSE_CARD: CardRule("approve", "expense.report", money=False),
    year_close.REOPEN_CARD: year_close.REOPEN_RULE,
    advances.ADVANCE_CARD: advances.RULE,
    "expenses.duplicate_drop": CardRule("approve", "expense.report", money=False),
    "expenses.attribute_receipt": CardRule("approve", "expense.report", money=False),
    "expenses.unknown_person_folder": CardRule("approve", "expense.report", money=False),
    "expenses.match_ambiguous": CardRule("approve", "expense.report", money=False),
    "expenses.qbo_map_payee": CardRule("approve", "books", money=False),
    "expenses.qbo_map_project_account": CardRule("approve", "books", money=False),
    "expenses.qbo_map_category": CardRule("approve", "books", money=False),
    "expenses.qbo_duplicate_review": CardRule("approve", "books", money=False),
}
