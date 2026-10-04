# Agent brief: close (the closer)

Design: `docs/closer-design.md` (approved 2026-07-21). The closer drives the
monthly close ceremony: it enumerates exactly what stands between now and a
sealed month, re-verifies on every re-run (repeat-until-green is the
expected rhythm), renders the evidence, and — only after the owner's
sign-off, only through the approval queue — performs the one write that
seals the period.

## What it does

- `preflight` — runs the close checklist for a month (default: the previous
  calendar month) and writes `CLOSE_PREFLIGHT.md` to the tenant's report
  dir. Every check is deterministic and month-scoped; each yields
  OK / WARN / BLOCK with one evidence line. A crashed check reports BLOCK;
  the preflight itself never dies of one bad check.
- `packet` — renders the close packet workbook (checklist + sign-off, P&L,
  balance sheet, agings, bank tie-out, payroll, owner transactions).
  Refuses to render over a month with any BLOCK.
- `lock` — the seal, owner-executes-UI + engine-verifies (redesigned
  after incident 2026-08-03: the accounting system's API silently ignores
  book-close-date writes — the field is UI-only, so the engine NEVER
  writes it). First run parks the `close.lock_period` card (open WARNs
  ride on it, so approving is an informed acceptance; refused over a
  BLOCK month); the card instructs the exact UI act (set the book-close
  date to month-end, Settings -> Advanced -> Accounting). After the owner
  approves and makes the UI change, the next run verifies by readback and
  records the seal; an unverified readback never records — it re-instructs
  and flags `close.seal_unverified`. The readback date is in the run key,
  so the owner's UI act breaks the "awaiting" replay. Sealed months with
  no ceremony in flight noop; shadow never records; a rejected card plus
  a re-run means the owner is asking again.
- `statements` — the ceremony's closing act (2026-08-03): renders the
  sealed month's P&L (month + YTD) and balance sheet as one statements
  workbook in the month folder, reveals it in the OS file browser when the
  tenant asks (`reveal_in_finder`), and emails it to the tenant's
  configured reviewers. Refuses over an unsealed month — the seal is the
  gate. The send is the agent's one external send and rides a
  `close.send_statements` card; the `close.statements_sent` event is the
  replay guard, so an approved card fires exactly once (a fresh card is a
  fresh ask, which is how corrected statements go out).

The ceremony: `engine close-preflight <tenant> --statement-balance <n>`
repeat-until-green, `--packet` for the evidence workbook, review, then
`engine run <tenant> close lock` + queue approval + one more lock run,
then `engine run <tenant> close statements` + queue approval + one more
statements run to send the review copies.

## What it never does

Guess at money. Post journal entries (adjustments are proposed as text; the
owner enters them). Write anywhere outside the report dir without the
guard's say. Seal a period without an approved card. Send mail without an
approved card. Read the auditor's CODE — check 1 reads the auditor's store
as data on disk; the checker never borrows the worker's code and the worker
never borrows the checker's authority.

## Judgment calls parked for the owner

The preflight parks nothing in the queue by itself; it is a look, not a
mutation. This agent's approvals are `close.lock_period` (seal the month)
and `close.send_statements` (email the statements to the reviewers).
Everything the preflight finds is the owner's to-do list, resolved in the
book (QBO, `engine status`, coding decisions) and re-verified by the next
run.
