# The closer — design

Status: BUILT (PRs #68-#72). All nine checks live, the packet renders, and
the lock is wired and approval-gated. The closer replaces a legacy pre-flight
script that ran six checks, three of them stubs that always passed, and left
the actual close to the owner by hand. It is designed for the engine as it
stands: the engine is the AP book of record, the accounting system carries the
bank feed and the engine's own bills, the auditor checks the machinery
nightly, and the owner signs off the close.

## What a close IS for this book

The monthly seal. At close, the owner can say four things with evidence:
the month is COMPLETE (every bank line booked, every payroll landed, every
known payable recorded), COHERENT (the engine ledger, the delivered
workbook, and the accounting system tell one story), REVIEWED (P&L and
balances read and understood, owner items treated consistently), and
SEALED (the accounting period locked so nothing drifts under it). The
closer's job is to drive that ceremony to done and produce the evidence —
never to guess at money.

## Position beside the auditor — worker, not checker

The auditor asks nightly "is the machinery telling the truth?" and fixes
nothing. The closer asks monthly "is the month finished?" and DRIVES the
finishing: it enumerates exactly what stands between now and a sealed
month, re-verifies on every re-run, renders the evidence packet, and (only
after sign-off, only through the approval queue) performs the one write
that seals the period.

The closer is therefore an ENGINE agent (`core/agents/close/`), on the
worker side of the independence boundary: it uses engine code freely —
ledger, QBO adapter, registry, guard — and the auditor audits the closer's
output like everything else the engine does. Symmetry worth stating: the
checker never borrows the worker's code; the worker never borrows the
checker's authority. A clean close report is a claim; the auditor's clean
nightly is the corroboration.

## The close checklist (month-scoped, all deterministic)

`engine run <tenant> close preflight --param month=YYYY-MM` (month defaults
to the previous calendar month). Each check yields OK / WARN / BLOCK with
one evidence line; the run is idempotent per (month, inputs) and safe to
repeat all week — the expected rhythm IS repeat-until-green.

1. **Machinery clean.** The auditor's open checklist carries no CRITICAL
   item. Read from the auditor's store read-only AS DATA (a file on disk,
   like any report — no code import in either direction). A close over a
   book the nightly checker distrusts is theater. CRITICAL open → BLOCK.
2. **Statement anchor.** The statement's ending balance ties to the QBO
   bank register balance at month-end. Build-time correction (2026-07-21)
   to the draft's "plus outstanding checks" arithmetic: in THIS book the
   register is feed-driven — a mailed check enters QBO only when it
   clears — so register and statement are both cleared-only views and the
   anchor is straight EQUALITY. The engine's committed-not-cleared checks
   are rendered as context (money already promised, visible nowhere in
   QBO yet), not as a reconciling term. The statement's numbers stay the
   bank's own truth (owner decision 2026-07-11: the independent
   soft-close anchor); where the bank delivers the statement
   INTO QBO's statements tab — see the inputs section for how the one
   number enters. Unexplained delta → BLOCK, delta in the evidence.
3. **Feed acceptance complete.** No bank-feed line for the month is still
   sitting unaccepted. The For-Review queue and statement lines have no
   API, so this is inferred, not enumerated: an unexplained delta in
   check 2 IS unaccepted (or misbooked) lines in aggregate, and the
   close-time look at the Banking tab ("N to review") confirms zero
   directly. Inferred debt → BLOCK until the register explains the
   statement to the cent.
4. **AP tie-out.** The engine ledger and QBO agree at month scope: every
   ledger row Paid in the month carries clearing evidence; open ledger
   payables reconcile to QBO's AP aging; no engine Bill unaccounted. The
   auditor does row-level nightly; this is the month-total tie-out that
   goes in the packet. Drift → BLOCK.
5. **Payroll landed.** Both semi-monthly payroll journal entries for the
   month exist in QBO books (the payroll provider's own QBO sync writes them; detected by
   date + descriptor, never guessed). A catch-up month carries extra
   payrolls — the check lists every payroll JE it found so
   the owner confirms the set. Missing JE → BLOCK.
6. **Owner transactions enumerated.** Every owner-shaped transaction in
   the month (owner reimbursements, owner-loan repayments) listed with its current coding. The treatment
   question (reimbursement vs loans-payable) is a standing open item; v1
   surfaces the list for consistent owner decision, WARN until each line
   is coded non-default. The advisory voice (auditor side) counsels;
   the closer only enumerates.
7. **Categorization swept.** No month transaction sits on Uncategorized
   Expense/Income/Asset or Ask My Accountant. Any → WARN with the list
   (BLOCK if the total exceeds a config threshold, default $500).
8. **Expenses folder.** Receipts present in `03_Expenses/<month>/` that
   no expense report covers → WARN, filing reminder only (the expenses agent's
   future lane; never blocks, same as the legacy close script).
9. **AR snapshot.** QBO AR aging for the month plus invoice-register
   pairing (every issued invoice row has its PDF on disk — old check 6
   carried forward). WARN-class in v1; the AR vertical is a later phase.

## Inputs: the statement lives in QBO (owner decision 2026-07-21)

No file is ever fed to the closer. The first tenant's bank delivers the
monthly statement into QBO's statements tab, so the bank's
truth is already inside the accounting system — but the API exposes
neither the statements tab nor the For-Review queue, so ONE number
crosses by hand at close time: the statement ending balance, read off
the QBO statement during the close sit-down (the owner reads it, or the
agent reads it through the owner's browser session, the same pattern as
the 2026-07-16 bank-rules session) and passed as
`--param statement_balance=12345.67`. Everything else — register
balance, booked lines, outstanding checks — the closer computes itself
from the API and the ledger. The earlier CSV-drop idea is dead: the
statement already arrives where the books live, and a second copy would
just be a second thing to keep honest.

Config: `[close]` section in tenant.toml — uncategorized BLOCK
threshold, the bank account's QBO name, report dir.

## Output contract

- **CLOSE_PREFLIGHT.md** at `05_Reports/YYYY-MM/CLOSE_PREFLIGHT.md` — the
  familiar name and place from the legacy close script, kept on purpose. Checks in
  order, status + one evidence line each, unresolved items as a to-do
  list. Exit codes: 0 all OK, 1 WARN present, 2 BLOCK present (old
  the legacy close contract, kept).
- **The close packet**, rendered only when preflight has no BLOCK:
  `05_Reports/YYYY-MM/Close_Packet_YYYY-MM.xlsx` (openpyxl, author and
  company = the tenant's legal name per invariant 10). Sheets: P&L for the month,
  balance sheet at month-end, AP aging, AR aging, bank tie-out
  (statement vs books, line counts and totals), payroll JEs, owner
  transactions, and a checklist sheet with a sign-off line. Numbers come
  from QBO report reads and the ledger in integer cents; the packet is a
  delivery view, regenerated on every run, never hand-edited.
- No external sends from the preflight or the packet. They land on disk;
  the owner reads them there.
- **The financial statements** (added 2026-08-03, owner ask after the
  first live close): `05_Reports/YYYY-MM/Financial_Statements_YYYY-MM.xlsx`
  — P&L for the month, P&L year to date, balance sheet at month-end,
  formatted as statements (numeric cells, accounting format, ruled bold
  totals, title block). Rendered by `close statements` ONLY over a sealed
  month: the seal is the gate, so the statements are the ceremony's
  conclusion, never a draft. When the tenant sets `reveal_in_finder`, a
  live render pops the OS file browser on the workbook (the owner runs
  the ceremony at the machine); "revealed in Finder" is claimed only when
  `open -R` exited 0 (a headless launchd context or a missing path exits
  non-zero, and the run says nothing). When `statements_recipients` is
  set, the job parks a `close.send_statements` card; on approval the next
  run emails the workbook from the tenant's mailbox (Graph `sendMail`, the
  mail adapter's one write, `Mail.Send` scope already in the consent) and
  records `close.statements_sent` — the event is the replay guard, one
  send per card. A fresh card is a fresh ask (corrected statements
  re-send by rejecting the old card and approving a new one).
  The send is at-most-once (issue #135) in a fixed order: build the mail
  client with its token in hand, stamp `send_started` on the card, POST.
  The token is acquired eagerly in the client factory, so a missing or
  expired mailbox consent (`GraphAuthError`) surfaces BEFORE the stamp:
  the run reports `close.statements_send_not_attempted` as an error,
  stamps nothing, and the next run sends normally against the same
  approved card. Only a failure AFTER the stamp is an unconfirmed send:
  the next run refuses to resend, flags `close.statements_send_unconfirmed`,
  and parks a fresh card (the owner checks Sent Items first).

## The seal (owner-executes-UI + engine-verifies)

REDESIGNED 2026-08-17 after the first live close (incident 2026-08-03):
the QBO API accepts a Preferences update and silently ignores
`BookCloseDate` — the field is UI-only, so the engine NEVER writes it.
After the owner reviews the packet and says go, `close lock` parks ONE
approval card: `close.lock_period` for YYYY-MM, whose text instructs the
exact UI act (set the book-close date to the month's last day, Settings ->
Advanced -> Accounting). The owner approves, makes the UI change, and
re-runs lock; the engine verifies by readback and records the sealed
event. An unverified readback never records — the run re-instructs and
flags `close.seal_unverified`. The `close.locked` payload carries both
`close_date` (the asked month end) and `readback` (the date the
accounting system actually returned): a readback LATER than the month end
still seals (the books are closed through at least that day, whether the
owner meant two months in one act or mistyped) but records
`close.seal_date_overshoots` naming both dates, and the summary says so,
so the ledger never claims a date the accounting system never held. The
readback date is part of the lock run
key, so the owner's UI act is what breaks the "awaiting" replay. Still
gated per invariant 7 and reversible in the UI — a two-way door,
deliberately the smallest possible seal, now with zero write pretending
to work. Adjusting entries, if the review surfaces any, are proposed as
text in the preflight and entered by the owner in v1; the closer never
posts a journal entry.

## New adapter surface (read side, plus one write)

`core/adapters/qbo.py` grows: `fetch_report(name, params)` for the QBO
Reports API (ProfitAndLoss, BalanceSheet, AgedPayables, AgedReceivables),
`fetch_account_transactions(account, month)` for the booked-lines diff,
`fetch_journal_entries(month)` for payroll detection, and
`fetch_book_close_date()` (the seal's verification read; the write-back
variant was removed 2026-08-17 — the API ignores it, see the seal
section).
All money through Decimal to integer cents, per house rule.
`core/adapters/graph_mail.py` grows `send_mail` (2026-08-03): the mail
adapter's one write, called only from behind an approved
`close.send_statements` card.

## Explicitly out of scope (v1)

Posting journal entries (accruals, depreciation, reclasses — proposed as
text, entered by the owner), the AR vertical (issue-side stays with the
register and its own lane), sales tax (a tenant setting, none for the first tenant), 1099/W-9
year-end prep (the auditor's advisory appendix owns it), payroll-provider API
integration (QBO journal entries are the evidence; the payroll system
stays the owner's), and any notification channel beyond the statements
review email (the one card-gated send added 2026-08-03).

## Build plan (starts on design approval; July close ~Aug 1)

1. Adapter report-reads + `close` agent skeleton; checks 1, 4, 7 (ledger
   + QBO only, no statement needed) with evals. CLOSE_PREFLIGHT.md
   renderer + exit codes.
2. Checks 2, 3: the bank-rec arithmetic (register + engine-known
   outstanding checks vs the statement_balance param).
3. Checks 5, 6, 8, 9 (payroll, owner items, expenses, AR snapshot).
4. Close packet renderer (openpyxl, invariant 10 metadata).
5. `close lock` + the approval card + `set_book_close_date` write,
   readback-verified.
6. FIRST LIVE CLOSE: July 2026, driven together with the owner in the
   Aug 1 window — the closer runs, the owner decides, the book seals.
   Lessons become evals before the August close (invariant 8 applied to
   the closer itself). DONE 2026-08-03.
7. `close statements` (owner ask, same day as the first live close):
   post-seal statements render + Finder reveal + card-gated review email.
