# Brief agent brief

## Mission

Give each person one plain page a week that leads with what needs them, so
they never have to open a dashboard to learn what the back office is waiting
on. This is Tim's first voice (Ask Tim build order #2, 2026-10-04): the
facts are the engine's, the page is the arrangement.

## What it does

- **weekly** reads the books through the read-only tools (core/tools) and
  writes `brief-<ISO week>.md` to `[brief].dir`: Needs you (cards waiting
  for a yes, deadlines overdue or in their final week), Coming up (deadlines
  inside their reminder windows, so the 90- and 30-day nudges ride here),
  Money (open payables and their total, expense reports not yet cleared),
  and The engine (any job whose last run ended in error, the last sealed
  month). A quiet week says so.
- One page per ISO week: Monday's first run writes it, later runs that week
  only work the send card, so the page that goes out is the page that was
  approved.
- With `[brief].recipients` set, an approval card asks to email it; an
  approved card sends exactly once (send_started is stamped first, and a
  stamp with no sent event is never resent). A tenant's own policy may name
  the send as unattended: `[brief].unattended = ["send"]`.

## Boundaries (never)

- Never computes a figure of its own or calls a model; every number comes
  from the tools, exact.
- Never sends without an approved card or the tenant's own unattended
  policy; never sends twice for one week.
