"""Bank-data adapter: the monthly statement PDF (phase 7 row 7.4, issue #213).

The statement is the bank's own record of what cleared, and it arrives every
month with no owner effort at all. A CSV export arrives only when somebody
remembers to make one — on the live tenant, once in nine months. So the PDF
is the primary statement feed and the CSV is optional evidence beside it;
both normalize to :class:`~core.adapters.bank_csv.BankLine`, and everything
downstream (identity, dedupe, the reconcile decision tiers) is unchanged.

**A single-bank reference parser.** The section headers, the check-grid
marker and the page-footer code it skips are one bank's statement layout,
the first tenant's. Another bank's statement is another parser behind the
same :class:`~core.adapters.bank_csv.BankLine` output, and a PDF this one does
not recognize raises :class:`StatementNotRecognized` rather than guessing.

Three sections, in the order the text carries them, ending at the daily
balance table:

1. the check grid — up to three cells per line, each ``number[*][i|s] MM/DD
   amount``. A ``0000`` number is the bank saying it could not read the MICR
   line: the line is real money, so it is parsed and carries no reference
   rather than an invented one.
2. ``Withdrawals / Debits`` — ``MM/DD amount free text``, money out.
   Descriptions wrap onto the next line.
3. ``Deposits / Credits`` — the same row shape, money in.

Every section header states a count and a total, and the parse must tie to
both or raise :class:`BankStatementError`. A statement half-read is worse
than a statement unread: the lines it dropped look exactly like money that
never moved. The reconcile job turns the raise into an anomaly, never a
silent skip.

Only check lines are clearing evidence today (``statement_evidence`` keeps
that rule: money out with a reference the ledger can carry). Withdrawals and
deposits are parsed, tied, counted, and recorded so the AR design day and
the bank sweep have the bank's own numbers on record at no extra cost.

Listing the folder lives here too, and it deliberately lets OSError out.
2026-09-13: macOS gates ``readdir`` on the Desktop tree per program
identity, a stat by name still succeeds, and the denial is silent — the
morning's zsh glob came back empty and reconcile logged the benign "no
statement export this run" while every engine-written payment waited on
exactly that file. A folder the engine cannot look inside is a failure of
the morning, never an empty answer.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .bank_csv import BankCsvFormat, BankLine


class BankStatementError(ValueError):
    """The statement was read but does not tie to its own printed totals."""


class StatementNotRecognized(BankStatementError):
    """This PDF is not a bank statement (no marker, no section headers).

    A folder of statements collects strays — a card statement, a drawing
    somebody dropped there. Under a loose glob they match by name, so the
    tier names them in the log and moves on rather than failing the run.
    """


# The bank's own legend under the check-grid header. Its presence is the
# cheapest proof that a PDF is one of these statements.
DEFAULT_MARKER = "Indicates gap in check sequence"

CHECKS = "Checks"
WITHDRAWALS = "Withdrawals / Debits"
DEPOSITS = "Deposits / Credits"
END_OF_SECTIONS = "Daily Balance Summary"

# MULTILINE so the same patterns serve both the line-by-line walk below and
# the "is this even a statement?" search over the whole text.
_SECTION_HEADERS = {
    CHECKS: re.compile(r"^Checks\s+(\d+)\s+checks?\s+totaling\s+\$([\d,]+\.\d{2})\s*$", re.M),
    WITHDRAWALS: re.compile(
        r"^Withdrawals\s*/\s*Debits\s+(\d+)\s+items?\s+totaling\s+\$([\d,]+\.\d{2})\s*$",
        re.M,
    ),
    DEPOSITS: re.compile(
        r"^Deposits\s*/\s*Credits\s+(\d+)\s+items?\s+totaling\s+\$([\d,]+\.\d{2})\s*$",
        re.M,
    ),
}
_CONTINUED = {
    CHECKS: re.compile(r"^Checks\s*-\s*continued\s*$"),
    WITHDRAWALS: re.compile(r"^Withdrawals\s*/\s*Debits\s*-\s*continued\s*$"),
    DEPOSITS: re.compile(r"^Deposits\s*/\s*Credits\s*-\s*continued\s*$"),
}

# One cell of the check grid: number, optional sequence-gap star, optional
# image/substitute marker, the paid date, the amount.
_CHECK_CELL = re.compile(r"(?<![\d.,])(\d{3,})\*?\s*(?:[is]\s+)?(\d{2}/\d{2})\s+([\d,]+\.\d{2})")
# One withdrawal or deposit row.
_TXN_ROW = re.compile(r"^(\d{2}/\d{2})\s+([\d,]+\.\d{2})\s*(.*)$")

# Column headers and page furniture that appear inside a section's lines. A
# continuation rule that swallowed these would paste "Page 2 of 2" onto a
# vendor's memo.
_FURNITURE = (
    re.compile(r"^Number\s+Date\s+Paid\s+Amount\b"),
    re.compile(r"^Date\s+Amount\s+Description\s*$"),
    re.compile(r"^\*\s*Indicates gap in check sequence"),
    re.compile(r"^Page\s+\d+\s+of\s+\d+\s*$"),
    re.compile(r"^FTCSTMT\S*\s"),
    re.compile(r"^\s*$"),
)

# ``Statement Period Date: 8/1/2026 - 8/31/2026`` — the fallback when the
# file name carries no date.
_PERIOD_LINE = re.compile(
    r"Statement Period Date:\s*\d{1,2}/\d{1,2}/(\d{4})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})"
)
_NAME_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s*$")

CHECK_DESCRIPTION = "CHECK"
UNREAD_CHECK_DESCRIPTION = "CHECK (number not read by bank)"


@dataclass
class StatementSections:
    """One statement's parsed lines, kept by section.

    The sections are separate because they mean different things to the
    engine: checks are clearing evidence, withdrawals are what the bank
    sweep parks, deposits are the AR lane's cleared-receipt ground truth.
    """

    period_end: date
    checks: list[BankLine] = field(default_factory=list)
    withdrawals: list[BankLine] = field(default_factory=list)
    deposits: list[BankLine] = field(default_factory=list)
    floor_dropped: int = 0

    @property
    def lines(self) -> list[BankLine]:
        return [*self.checks, *self.withdrawals, *self.deposits]

    def counts(self) -> dict[str, int]:
        return {
            "checks": len(self.checks),
            "withdrawals": len(self.withdrawals),
            "deposits": len(self.deposits),
            "floor_dropped": self.floor_dropped,
        }


def _cents(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _iso(mmdd: str, period_end: date) -> str:
    month, day = (int(p) for p in mmdd.split("/"))
    # Statements are calendar months, so every line takes the period's year.
    # The guard is for a period that straddles a rollover: a line dated a
    # month later than the period end belongs to the year before.
    year = period_end.year - 1 if month > period_end.month else period_end.year
    return date(year, month, day).isoformat()


def _furniture(line: str) -> bool:
    return any(pattern.match(line) for pattern in _FURNITURE)


def _tie(section: str, lines: list[BankLine], expected: tuple[int, Decimal] | None) -> None:
    """A section must tie to the count and total the bank printed above it."""
    if expected is None:
        return
    count, total = expected
    parsed_total = sum((abs(ln.amount) for ln in lines), Decimal("0"))
    if len(lines) != count or parsed_total != total:
        raise BankStatementError(
            f"{section} does not tie: the statement says {count} line(s) totaling "
            f"${total:,.2f}, the parse found {len(lines)} totaling ${parsed_total:,.2f}"
        )


def parse_statement_sections(
    text: str,
    *,
    period_end: date,
    floor: date | None = None,
) -> StatementSections:
    """Parse one statement's text into its three sections.

    ``period_end`` supplies the year every ``MM/DD`` is missing. ``floor``
    drops lines the closed books already answered — AFTER the tie, because
    the tie is against what the bank printed, not against what this engine
    cares about.
    """
    sections = StatementSections(period_end=period_end)
    buckets: dict[str, list[BankLine]] = {CHECKS: [], WITHDRAWALS: [], DEPOSITS: []}
    expected: dict[str, tuple[int, Decimal] | None] = {
        CHECKS: None,
        WITHDRAWALS: None,
        DEPOSITS: None,
    }
    seen_header = False
    current: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        matched = False
        for name, pattern in _SECTION_HEADERS.items():
            found = pattern.match(line)
            if found:
                current, seen_header, matched = name, True, True
                expected[name] = (int(found.group(1)), _cents(found.group(2)))
                break
        if matched:
            continue
        if any(p.match(line) for p in _CONTINUED.values()):
            current = next(n for n, p in _CONTINUED.items() if p.match(line))
            seen_header = True
            continue
        if line.startswith(END_OF_SECTIONS):
            current = None
            continue
        if current is None or _furniture(line):
            continue

        if current == CHECKS:
            for number, mmdd, amount in _CHECK_CELL.findall(line):
                unread = set(number) == {"0"}
                buckets[CHECKS].append(
                    BankLine(
                        date=_iso(mmdd, period_end),
                        description=UNREAD_CHECK_DESCRIPTION if unread else CHECK_DESCRIPTION,
                        check_ref="" if unread else number,
                        amount=-_cents(amount),
                    )
                )
            continue

        row = _TXN_ROW.match(line)
        if row:
            amount = _cents(row.group(2))
            buckets[current].append(
                BankLine(
                    date=_iso(row.group(1), period_end),
                    description=row.group(3).strip(),
                    check_ref="",
                    amount=amount if current == DEPOSITS else -amount,
                )
            )
        elif buckets[current]:
            # A wrapped description: the bank breaks a long memo mid-field.
            last = buckets[current][-1]
            last.description = f"{last.description} {line.strip()}".strip()

    if not seen_header:
        raise StatementNotRecognized(
            "no Checks / Withdrawals / Deposits section header: this is not a bank statement"
        )
    for name in (CHECKS, WITHDRAWALS, DEPOSITS):
        _tie(name, buckets[name], expected[name])

    kept = 0
    for name, target in (
        (CHECKS, sections.checks),
        (WITHDRAWALS, sections.withdrawals),
        (DEPOSITS, sections.deposits),
    ):
        for ln in buckets[name]:
            if floor is not None and date.fromisoformat(ln.date) < floor:
                sections.floor_dropped += 1
                continue
            target.append(ln)
            kept += 1
    return sections


def parse_statement_text(
    text: str, *, period_end: date, floor: date | None = None
) -> list[BankLine]:
    """Every line of one statement, checks first (the pure parse seam)."""
    return parse_statement_sections(text, period_end=period_end, floor=floor).lines


def pdf_text(path: Path | str) -> str:
    """The text layer of a PDF, page by page. The one read seam, so a test
    can prove which files the tier opens."""
    from pypdf import PdfReader

    from core.engine.timebox import ParseTimeout, pdf_deadline

    try:
        with pdf_deadline(Path(path).name):
            return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except ParseTimeout as exc:
        raise BankStatementError(f"{exc} (a crafted or broken file)") from exc


def period_end_from_name(path: Path | str) -> date | None:
    """The trailing ``YYYY-MM-DD`` in a statement's file name, or None."""
    found = _NAME_DATE.search(Path(path).stem)
    if not found:
        return None
    try:
        return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    except ValueError:
        return None


def _period_end_from_text(text: str) -> date | None:
    found = _PERIOD_LINE.search(text)
    if not found:
        return None
    try:
        return date(int(found.group(4)), int(found.group(2)), int(found.group(3)))
    except ValueError:
        return None


def statement_floor(fmt: BankCsvFormat) -> date | None:
    """The earliest date that can be evidence, or None for no floor."""
    raw = str(getattr(fmt, "statement_floor", "") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BankStatementError(
            f"[bank_csv].statement_floor {raw!r} is not an ISO date (YYYY-MM-DD)"
        ) from exc


def read_statement_pdf(path: Path | str, fmt: BankCsvFormat) -> StatementSections:
    """One statement file, parsed and tied, floored by tenant config."""
    path = Path(path)
    text = pdf_text(path)
    marker = str(getattr(fmt, "statement_pdf_marker", "") or DEFAULT_MARKER)
    if marker and marker not in text and not any(p.search(text) for p in _SECTION_HEADERS.values()):
        raise StatementNotRecognized(
            f"{path.name}: no {marker!r} marker and no section header; not a bank statement"
        )
    period_end = period_end_from_name(path) or _period_end_from_text(text)
    if period_end is None:
        raise BankStatementError(
            f"{path.name}: no statement period — the file name carries no trailing "
            "YYYY-MM-DD and the text carries no 'Statement Period Date:' line"
        )
    try:
        return parse_statement_sections(text, period_end=period_end, floor=statement_floor(fmt))
    except StatementNotRecognized as exc:
        raise StatementNotRecognized(f"{path.name}: {exc}") from exc
    except BankStatementError as exc:
        raise BankStatementError(f"{path.name}: {exc}") from exc


def parse_statement_pdf(path: Path | str, fmt: BankCsvFormat) -> list[BankLine]:
    """Every line of one statement PDF (the thin wrapper over the parse)."""
    return read_statement_pdf(path, fmt).lines


def statement_files(directory: Path | str, fmt: BankCsvFormat) -> list[Path]:
    """The statement files in ``directory``, sorted by name.

    Every ``*.csv`` (the optional export) plus every PDF matching
    ``[bank_csv].statement_pdf_glob``. A PDF whose period ends before
    ``[bank_csv].statement_floor`` is left out here, so a closed year's
    statements are never even opened.

    OSError propagates on purpose (2026-09-13): a folder that cannot be
    listed is an environment failure, never "no statement this morning".
    """
    directory = Path(directory)
    glob = str(getattr(fmt, "statement_pdf_glob", "") or "*.pdf")
    floor = statement_floor(fmt)
    picked: list[Path] = []
    for entry in sorted(directory.iterdir()):  # raises on a refused readdir
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix == ".csv":
            picked.append(entry)
        elif suffix == ".pdf" and fnmatch.fnmatch(entry.name, glob):
            period_end = period_end_from_name(entry)
            if floor is not None and period_end is not None and period_end < floor:
                continue
            picked.append(entry)
    return picked


def parse_statement_file(path: Path | str, fmt: BankCsvFormat) -> StatementSections:
    """One statement file of either kind, normalized to sections.

    A CSV carries no section structure, so its lines are bucketed the way
    the bank would have printed them: money out with a reference is a check,
    money out without one is a withdrawal, money in is a deposit.
    """
    from .bank_csv import parse_bank_csv

    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return read_statement_pdf(path, fmt)
    floor = statement_floor(fmt)
    period_end = period_end_from_name(path) or date.today()
    sections = StatementSections(period_end=period_end)
    for line in parse_bank_csv(path, fmt):
        if floor is not None and _line_date(line) < floor:
            sections.floor_dropped += 1
            continue
        if line.amount > 0:
            sections.deposits.append(line)
        elif line.check_ref.strip():
            sections.checks.append(line)
        else:
            sections.withdrawals.append(line)
    return sections


def _line_date(line: BankLine) -> date:
    try:
        return date.fromisoformat(str(line.date)[:10])
    except ValueError:  # pragma: no cover - the CSV adapter emits ISO dates
        return datetime.min.date()
