# Timesheets agent brief

## Mission

Own the intake of team timesheet submissions so no submitted hours are ever
lost or hand-shuffled: recognize submissions in the landing folder, parse
them deterministically, record them in the ledger, file the human-readable
copies into the tenant's Timesheets structure, and put a payroll-ready hours
summary in front of the owner before each pay run.

Built 2026-07-11 after a discovery during the AP cutover: the prior
timesheet processor had been idle for a month and the owner had been pulling
hours by ad-hoc prompt, believing automation was doing it.

## What it does

- **intake**: scans the landing folder's top level for files matching the
  form's naming (``timesheet_<person>_<week-ending>_<id>.csv/.xlsx``). The
  CSV parses with pure code (no model call; the format is fixed by the
  form). Each new submission is recorded as ledger events keyed by content
  hash, both file copies are filed to ``<timesheets filing dir>/YYYY-MM/``
  (never overwriting; collisions get a suffixed name), and one approval
  card per submission (``timesheets.payroll_hours``) is queued with person,
  week ending, total hours, and the per-project breakdown.

## Boundaries (never)

- Never touches any payroll or accounting system; the approval card is a
  human-facing summary, and entering hours anywhere is the owner's act
  (invariant 7).
- Never edits the tenant's existing timesheet/payscale workbook; the agent
  files copies of submissions and records events only.
- Never guesses at a malformed submission: a parse failure or an
  hours-total mismatch is an anomaly plus a flagged event, and the file
  stays visible at the landing top level for a human.
- Never moves or deletes a landing original (the AP janitor archives a
  submission only after this agent has recorded it).

## Escalation

- Parse failure or hours mismatch: anomaly ``timesheets.parse_failed``.
- Every successful submission: approval ``timesheets.payroll_hours`` (the
  owner reviews hours before the pay run; approval records the decision).

## Input / output contract

Input: the landing folder (shared with AP intake), tenant config
``[timesheets]`` (``landing_dir``, ``filing_dir``). Output: the standard
``RunResult``; events ``timesheets.recorded`` (CSV, full parse payload) and
``timesheets.companion_filed`` (XLSX twin); approvals as above. No SQL
tables: submissions live as events until reporting needs argue otherwise.
