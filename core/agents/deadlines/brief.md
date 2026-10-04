# Deadlines agent brief

## Mission

Surface every dated obligation weeks before it is due, so nothing that needs a
person (renew a passport, file a return, renew a registration, send a report)
is ever found the week it lapses. This is the engine's first **Reminder** lane
(docs/boundary-rules.md): the engine cannot do the act, so its job is to make
sure the act is never a surprise.

Built 2026-10-04 as the first of the three missionary-shaped capabilities
(the deadline calendar, then the weekly brief, then field books). The same
lane serves a small business: its filings, licences and expiring
credentials are obligations too.

## What it does

- **scan** reads `obligations.toml` beside the tenant file, works out each
  obligation's open occurrences (a recurring one rolls from its anchor; a
  month-end anchor clamps, never creeps), and records one
  `deadlines.reminder` event per lead window as it is crossed: once ever, at
  the tightest window crossed (an obligation added five days out says
  "7 days" once, never 90, 30 and 7 in one morning), and once more when it
  goes overdue. It rewrites the calendar file (`[deadlines].ics_path`), an
  all-day event per open occurrence with an alarm per lead window, so any
  calendar app shows it ahead. No file means the lane is off.
- **done** (`--param id=<id>`, optionally `due=YYYY-MM-DD`) records the
  owner's act. A one-off closes; a recurring obligation rolls to its next
  date.

- **calendar** writes each open deadline into the person's own calendar
  as an all-day event with the final week's reminder, because Outlook and
  Google drop the reminders inside a calendar file. Which calendar is
  decided in code (core/agents/deadlines/calendar_sync.py): the tenant's
  setting, the organization's Microsoft mail connection, the address's
  domain, its public mail records, else the calendar file. Each run creates
  what is new, updates what changed, and removes what is done or moved; the
  plan waits for an approval card unless `[deadlines].unattended` names
  `calendar` (the owner's one-time grant). v1 writes Microsoft calendars and
  needs Calendars.ReadWrite, granted once with
  `engine mail consent <tenant> --scope Calendars.ReadWrite` ([mail].scopes
  is untouched, so no mail job ever waits on the calendar); a missing permission is an anomaly that
  says so.

## Boundaries (never)

- Never calls a model or moves money. Reminders are ledger events, a
  calendar file, and (with approval) events in the person's own calendar.
- Never guesses a date. A malformed obligations file is an error that names
  the problem, not an empty calendar.
- Never marks anything done on its own. Done is the owner's act, recorded.

## Inputs and outputs

- In: `obligations.toml` (format in `schema.py`), `[deadlines]` in the tenant
  file (`file`, `ics_path`, `lead_days`, default 90/30/7).
- Out: `deadlines.reminder` and `deadlines.done` events; the calendar file.
