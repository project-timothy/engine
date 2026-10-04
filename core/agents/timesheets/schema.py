"""Timesheets agent: input contract and the deterministic CSV parser.

Submissions arrive from the tenant's timesheet web form as a CSV/XLSX pair
named ``timesheet_<person>_<week-ending>_<submit-id>.{csv,xlsx}``. The CSV is
the machine-readable source of truth; the XLSX is a human-friendly companion.
Parsing is pure code, no model call: the format is fixed by the form
(header key/value rows, then a per-project table, then a DAILY TOTALS row).
"""

from __future__ import annotations

import csv
import io
import re

from pydantic import BaseModel, Field

# The form's file naming. Generic convention, no tenant named.
TIMESHEET_FILE_RE = re.compile(
    r"^timesheet_.+_\d{4}-\d{2}-\d{2}_\d{8}-[A-Z0-9]{6}\.(csv|xlsx)$", re.IGNORECASE
)


class TimesheetParseError(ValueError):
    """The file matched the timesheet naming but its content did not parse,
    or its per-project hours disagree with the declared total."""


class TimesheetLine(BaseModel):
    project: str
    description: str = ""
    hours: float


class TimesheetSubmission(BaseModel):
    person: str
    week_ending: str  # ISO date from the form
    submitted_at: str = ""
    total_hours: float
    lines: list[TimesheetLine] = Field(default_factory=list)

    @property
    def month(self) -> str:
        return self.week_ending[:7]


def is_timesheet_name(name: str) -> bool:
    return bool(TIMESHEET_FILE_RE.match(name))


def parse_timesheet_csv(text: str) -> TimesheetSubmission:
    """Parse the form's CSV. Raises :class:`TimesheetParseError` on any
    structural surprise or an hours mismatch, so a malformed submission is
    flagged for a human instead of silently miscounted (hours feed payroll).
    """
    header: dict[str, str] = {}
    lines: list[TimesheetLine] = []
    in_table = False
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        first = row[0].strip()
        if not in_table:
            if first.lower() == "project":
                in_table = True
                continue
            if len(row) >= 2:
                header[first.lower()] = row[1].strip()
            continue
        if first.upper() == "DAILY TOTALS":
            break
        try:
            hours = float(row[-1])
        except (ValueError, IndexError) as exc:
            raise TimesheetParseError(f"unparseable hours on project row {first!r}") from exc
        lines.append(
            TimesheetLine(
                project=first, description=(row[1].strip() if len(row) > 1 else ""), hours=hours
            )
        )

    missing = [k for k in ("consultant", "week ending", "total hours") if k not in header]
    if missing:
        raise TimesheetParseError(f"missing header field(s): {', '.join(missing)}")
    try:
        total = float(header["total hours"])
    except ValueError as exc:
        raise TimesheetParseError("Total Hours is not a number") from exc
    if not lines:
        raise TimesheetParseError("no project rows found")
    if abs(sum(line.hours for line in lines) - total) > 0.01:
        raise TimesheetParseError(
            f"project hours sum {sum(line.hours for line in lines):g} != declared total {total:g}"
        )
    return TimesheetSubmission(
        person=header["consultant"],
        week_ending=header["week ending"],
        submitted_at=header.get("submitted", ""),
        total_hours=total,
        lines=lines,
    )
