"""The bank statement PDF adapter (phase 7 row 7.4, issue #213).

The monthly statement is the bank's own record and it lands every month
with zero owner effort, while a CSV export happens only when somebody
remembers to make one (once in nine months on the live tenant). So the PDF
is the primary statement feed and these tests pin its parse.

Three sections, in the order the text carries them, ending at the daily
balance table: the check grid (up to three cells per line), Withdrawals /
Debits, and Deposits / Credits. Every section header states a count and a
total, and the parse must tie to both or raise — a statement half-read is
worse than none, because the lines it dropped look like money that never
moved.

The fixture text is the one bank's real layout and markers with synthetic
numbers throughout, chosen so every section still ties to its printed count
and total; payee-bearing descriptions are demo names (invariant 5 keeps
tenant specifics out of core, and out of core's tests).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import minimal_pdf
from core.adapters.bank_csv import BankCsvFormat
from core.adapters.bank_statement_pdf import (
    BankStatementError,
    StatementNotRecognized,
    parse_statement_pdf,
    parse_statement_sections,
    parse_statement_text,
    period_end_from_name,
    statement_files,
)

MARKER = "* Indicates gap in check sequence i = Electronic Image s = Substitute Check"
GRID_HEADER = "Number Date Paid Amount Number Date Paid Amount Number Date Paid Amount"

# The August shape: eight checks in a three-column grid, one of them a 0000
# (the bank could not read the MICR line), seven withdrawals, one of which
# wraps onto a second line, three deposits.
AUGUST = f"""\
Statement Period Date: 8/1/2026 - 8/31/2026
Account Summary - 5550001234
8 Checks $(63,762.95)
Checks 8 checks totaling $63,762.95
{MARKER}
{GRID_HEADER}
0000 i 08/24 21,500.00 3054*i 08/31 18,245.60 7065*i 08/03 5,410.00
3050*i 08/11 1,862.35 7056*i 08/05 9,000.00 7066 i 08/18 2,740.00
3051 i 08/25 3,880.00 7057 i 08/05 1,125.00
FTCSTMT002 002 20260831 UDSPDFSTMT 0000000005550001234 DDA
Page 2 of 2
Withdrawals / Debits 7 items totaling $23,336.15
Date Amount Description
08/04 98.00 PAYROLL CO FEE 100001 Demo Company 080426
08/10 1,904.17 CARD ISSUER CRCARDPMT Demo Owner 081026
08/13 3,655.42 PAYROLL CO TAX 100002 Demo Company 081326
08/13 6,810.33 PAYROLL CO NET 100003 Demo Company 081326
08/24 245.60 SENT ZELLE PMT ID ZEL000000001 TO DEMO OWNER
08/28 3,720.15 DEBIT CARD PURCHASE AT DEMO BUREAU, ANYTOWN, US ON 082726 FROM CARD#:
XXXXXXXXXXXX0000
08/28 6,902.48 PAYROLL CO NET 100004 Demo Company 082826
Deposits / Credits 3 items totaling $131,445.39
Date Amount Description
08/14 92,815.40 DEMO CUSTOMER PAYMENTS FCS000000000001 DEMO COMPANY 081426
08/25 38,600.00 DEMO CUSTOMER PAYMENTS FCS000000000002 DEMO COMPANY 082526
08/31 29.99 DEPOSIT
Daily Balance Summary
Date Amount Date Amount Date Amount
08/03 201,500.00 08/11 188,245.60 08/24 240,118.40
"""

AUG_END = date(2026, 8, 31)


def _sections(text=AUGUST, *, period_end=AUG_END, floor=None):
    return parse_statement_sections(text, period_end=period_end, floor=floor)


# ---- the three sections ------------------------------------------------------


def test_check_grid_reads_every_cell_across_the_three_columns():
    checks = _sections().checks
    assert len(checks) == 8
    assert [c.check_ref for c in checks] == [
        "",  # the 0000 cell: the bank could not read the number
        "3054",
        "7065",
        "3050",
        "7056",
        "7066",
        "3051",
        "7057",
    ]
    by_ref = {c.check_ref: c for c in checks if c.check_ref}
    assert by_ref["7066"].date == "2026-08-18"
    assert by_ref["7066"].amount == Decimal("-2740.00")
    assert by_ref["7066"].description == "CHECK"
    assert by_ref["3054"].amount == Decimal("-18245.60")  # the * sequence-gap mark


def test_an_unread_check_number_is_named_not_guessed():
    """``0000`` is the bank saying it could not read the MICR line. The line
    is real money and must be parsed; inventing a number would be worse than
    carrying none, so it carries none and says why."""
    (unread,) = [c for c in _sections().checks if not c.check_ref]
    assert unread.amount == Decimal("-21500.00")
    assert unread.date == "2026-08-24"
    assert unread.description == "CHECK (number not read by bank)"


def test_withdrawals_are_negative_and_a_wrapped_description_is_joined():
    debits = _sections().withdrawals
    assert len(debits) == 7
    assert all(d.amount < 0 for d in debits)
    assert all(d.check_ref == "" for d in debits)
    wrapped = [d for d in debits if "DEMO BUREAU" in d.description]
    assert len(wrapped) == 1
    assert wrapped[0].description.endswith("FROM CARD#: XXXXXXXXXXXX0000")
    assert wrapped[0].amount == Decimal("-3720.15")


def test_deposits_are_positive():
    credits = _sections().deposits
    assert [c.amount for c in credits] == [
        Decimal("92815.40"),
        Decimal("38600.00"),
        Decimal("29.99"),
    ]
    assert credits[0].date == "2026-08-14"


def test_page_furniture_between_sections_is_never_a_description():
    """A section can break across a page, and the page header sits in the
    middle of the text. A continuation rule that swallowed it would paste
    'Page 2 of 2' onto a vendor's memo."""
    joined = " ".join(ln.description for ln in parse_statement_text(AUGUST, period_end=AUG_END))
    assert "FTCSTMT002" not in joined
    assert "Page 2 of 2" not in joined


def test_every_line_takes_the_year_from_the_period_end():
    lines = parse_statement_text(AUGUST, period_end=AUG_END)
    assert {ln.date[:4] for ln in lines} == {"2026"}
    # A statement whose period ends in January carries no December lines by
    # construction, but the guard is cheap and a December statement mailed in
    # January must never date its lines a year forward.
    december = AUGUST.replace("08/", "12/")
    lines = parse_statement_text(december, period_end=date(2027, 1, 31))
    assert {ln.date[:4] for ln in lines} == {"2026"}


def test_a_statement_with_no_checks_still_parses_the_other_sections():
    text = AUGUST.split("Checks 8 checks")[0] + AUGUST.split("Page 2 of 2\n")[1]
    sections = parse_statement_sections(text, period_end=AUG_END)
    assert sections.checks == []
    assert len(sections.withdrawals) == 7
    assert len(sections.deposits) == 3


def test_a_continued_section_keeps_one_count():
    """A long section repeats its header on the next page as '- continued',
    with no count of its own. The count on the first header covers both."""
    text = AUGUST.replace(
        "08/28 6,902.48 PAYROLL CO NET 100004 Demo Company 082826",
        "FTCSTMT002 002 20260831 UDSPDFSTMT 0000000005550001234 DDA\n"
        "Page 2 of 2\n"
        "Withdrawals / Debits - continued\n"
        "Date Amount Description\n"
        "08/28 6,902.48 PAYROLL CO NET 100004 Demo Company 082826",
    )
    assert len(parse_statement_sections(text, period_end=AUG_END).withdrawals) == 7


# ---- the self-check ----------------------------------------------------------


def test_a_count_that_does_not_tie_raises_and_names_the_section():
    text = AUGUST.replace("3051 i 08/25 3,880.00 7057 i 08/05 1,125.00", "")
    with pytest.raises(BankStatementError) as caught:
        parse_statement_sections(text, period_end=AUG_END)
    detail = str(caught.value)
    assert "Checks" in detail
    assert "8" in detail and "6" in detail


def test_a_total_that_does_not_tie_raises():
    text = AUGUST.replace("08/31 29.99 DEPOSIT", "08/31 39.99 DEPOSIT")
    with pytest.raises(BankStatementError) as caught:
        parse_statement_sections(text, period_end=AUG_END)
    assert "Deposits / Credits" in str(caught.value)


def test_text_without_the_marker_or_a_section_is_not_a_statement():
    """A card statement or an engineering drawing filed in the same folder."""
    with pytest.raises(StatementNotRecognized):
        parse_statement_sections("ACCOUNT SUMMARY\nSome other document\n", period_end=AUG_END)


# ---- the 2026 floor ----------------------------------------------------------


def test_lines_before_the_floor_are_dropped_after_the_tie():
    """The tie is against what the bank printed, so it runs on every line;
    the floor then drops what the closed books already answered."""
    sections = _sections(floor=date(2026, 8, 20))
    assert [c.check_ref for c in sections.checks] == ["", "3054", "3051"]
    assert sections.floor_dropped == 10
    assert all(ln.date >= "2026-08-20" for ln in sections.lines)


# ---- the file (pypdf, one text layer) ----------------------------------------


def _fmt(**kw) -> BankCsvFormat:
    return BankCsvFormat(statement_pdf_glob="Demo checking X1234 - *.pdf", **kw)


def _statement(dirpath, name: str, text: str = AUGUST):
    p = dirpath / name
    p.write_bytes(minimal_pdf(text))
    return p


def test_parse_statement_pdf_reads_the_text_layer(tmp_path):
    p = _statement(tmp_path, "Demo checking X1234 - 2026-08-31.pdf")
    lines = parse_statement_pdf(p, _fmt())
    assert len(lines) == 18  # 8 checks + 7 withdrawals + 3 deposits
    assert sum(1 for ln in lines if ln.check_ref) == 7


def test_period_end_comes_off_the_file_name():
    assert period_end_from_name("Demo checking X1234 - 2026-08-31.pdf") == AUG_END
    assert period_end_from_name("no date here.pdf") is None


def test_a_pdf_with_no_date_in_the_name_reads_its_own_period(tmp_path):
    p = _statement(tmp_path, "Demo checking X1234 - august.pdf")
    assert parse_statement_pdf(p, _fmt())[0].date.startswith("2026-08")


# ---- selection ---------------------------------------------------------------


def test_selection_takes_the_csvs_and_the_matching_pdfs(tmp_path):
    _statement(tmp_path, "Demo checking X1234 - 2026-07-31.pdf")
    _statement(tmp_path, "Demo checking X1234 - 2026-08-31.pdf")
    (tmp_path / "Demo checking X1234 - 2026-04-01_to_2026-05-12.csv").write_text("Date\n")
    (tmp_path / "20260101-card statement-0000.pdf").write_bytes(minimal_pdf("card"))
    (tmp_path / ".DS_Store").write_text("x")

    picked = [p.name for p in statement_files(tmp_path, _fmt())]

    assert picked == [
        "Demo checking X1234 - 2026-04-01_to_2026-05-12.csv",
        "Demo checking X1234 - 2026-07-31.pdf",
        "Demo checking X1234 - 2026-08-31.pdf",
    ]


def test_a_glob_match_that_is_not_a_statement_is_skipped_not_raised(tmp_path):
    """Under a loose glob (the default ``*.pdf``) the folder's strays match by
    name. A PDF with neither the check-grid marker nor a section header is
    some other document, and the tier says so and moves on."""
    stray = tmp_path / "ACME-SCH-SHT1-Model.pdf"
    stray.write_bytes(minimal_pdf("AS INSTALLED\nSHEET 1 OF 6\n"))
    assert [p.name for p in statement_files(tmp_path, BankCsvFormat())] == [stray.name]
    with pytest.raises(StatementNotRecognized):
        parse_statement_pdf(stray, BankCsvFormat())


def test_a_statement_before_the_floor_is_never_opened(tmp_path, monkeypatch):
    """2025's books are closed and sealed; those statements are never
    evidence, so the tier does not even read the bytes. Proven at the read
    seam, not by the answer: a parser that opened the file and filtered its
    lines afterwards would pass an answer-shaped assertion."""
    _statement(tmp_path, "Demo checking X1234 - 2025-12-31.pdf")
    _statement(tmp_path, "Demo checking X1234 - 2026-08-31.pdf")
    fmt = _fmt(statement_floor="2026-01-01")

    picked = statement_files(tmp_path, fmt)
    assert [p.name for p in picked] == ["Demo checking X1234 - 2026-08-31.pdf"]

    import core.adapters.bank_statement_pdf as mod

    opened: list[str] = []
    real = mod.pdf_text
    monkeypatch.setattr(mod, "pdf_text", lambda p: (opened.append(Path(p).name), real(p))[1])
    for path in picked:
        parse_statement_pdf(path, fmt)
    assert opened == ["Demo checking X1234 - 2026-08-31.pdf"]


def test_an_unreadable_folder_is_an_error_never_an_empty_answer(tmp_path):
    """2026-09-13: macOS gates readdir per program identity, stat by name
    still works, and the denial is silent. A folder the engine cannot look
    inside is a failure of the morning, never 'no statement this morning'."""
    d = tmp_path / "Statements"
    d.mkdir()
    (d / "Demo checking X1234 - 2026-08-31.pdf").write_bytes(minimal_pdf(AUGUST))
    d.chmod(0o311)
    try:
        with pytest.raises(OSError):
            statement_files(d, _fmt())
    finally:
        d.chmod(0o755)


def test_a_missing_folder_is_an_error_too(tmp_path):
    with pytest.raises(OSError):
        statement_files(tmp_path / "nope", _fmt())
