# W2 — BillPayment records (Build 3, phase 2) — design

Status: BUILT 2026-09-12 as phase 7 row 7.2 (issue #211, PR #245), behind
`[qbo].payment_records` (a tenant setting, off by default). LIVE for the
first tenant since row 7.3 (issue #212) the same day: the statement file
is the clearing signal, `qbo-push-payments` runs in the 08:00 sequence,
and dark mode is over. See "What shipped" and "Row 7.3: the statement
tier" at the end for the mechanics the builds settled.
This extends `qbo-write-side-design.md` (W1, live since 2026-07-20); the
invariants, mapping rules, and rollback contract there apply unchanged.

## Why now — the 2026-07-23 live case

The owner asked why cleared bank activity wasn't "bleeding through" to QBO.
Findings, all verified in-session:

- Four checks had cleared the bank over five days.
  All four sat in QBO's bank-feed For Review queue, invisible to the API,
  so reconcile ran green for 6 days while 7 ledger rows stayed `Scheduled`
  and the P&L was missing six figures of income and five of expenses.
- Clearing them took a human (this session, driving the QBO UI): two
  match-clicks against W1 bills (one check paying two invoices from one
  vendor; one check paying three from a contractor), two hand-coded expenses
  for pre-W1 bills with no QBO record, plus owner-side deposit coding.
- The match-clicks against W1 bills were trivial and correct on the first
  try — W1 did its job. The expensive part was everything that had no QBO
  record waiting to be matched.

W2 makes the record exist before the feed line arrives: when the ledger
knows a payment (owner schedules a check with a number), the engine records
the `BillPayment` in QBO, approval-gated. The feed line then shows up as a
one-click "Match" — no coding, no account-picking, no drift. 5 of the 7
stuck items in the live case would have arrived pre-matched.

The engine still never moves money (invariant 7). W2 writes records of
payments the owner already made.

## The owner decision the W1 doc deferred: clearing evidence

W1's design named the problem: once payment records are engine-written at
scheduling time, the owner's match-click no longer creates an API-visible
transaction, and rule 3 (reconcile must never read engine-authored records
as clearing evidence) means the evidence stream for those payments goes
silent. The QBO API cannot see For Review or per-transaction matched
status; nothing else in the API will ever say "cleared."

Decision needed at the gate: adopt **Option B — the bank CSV becomes the
clearing signal for engine-recorded payments.**

- Owner exports the bank's CSV on demand (~2 min) and at month-end;
  the closer's statement step already brings true cleared data monthly.
- The CSV adapter exists (`core/adapters/bank_csv.py`); reconcile gains a
  CSV evidence path alongside the QBO path (`--param bank_csv=...` is
  already plumbed for imports).
- Ledger rows flip `Scheduled -> Paid` from CSV evidence; QBO-side
  evidence continues to cover human- and rule-authored records exactly as
  today.

Practical cadence: freshness between exports is bounded by how often the
owner cares to export; the daily-run default stays QBO-evidence-only, so
nothing regresses if the CSV never shows up until close.

**Settled 2026-09-09 (the first tenant's sweep design, kept in its own repository):** Option A at zero owner
clicks. The Thursday For Review sweep is agent-executed in a browser
(the tenant's `host/qbo-sweep/`); the agent makes the match click on the
lines QBO proposes and the ledger corroborates, so feed acceptance stays the
clearing signal for human- and rule-authored records.

**Re-settled 2026-09-12 (phase 7 row 7.3):** once
the engine writes the BillPayment, the sweep's Match click attaches the feed
line to a record the API already showed and rule 3 already filters, so the
click changes nothing reconcile can see. Option B is back for exactly those
rows: the bank's statement file is the clearing signal for engine-recorded
payments (and for any open check group it names), beside QBO evidence for
everything else. The sweep keeps the books tidy; the statement settles the
ledger.

## Job design

`ap/qbo-push-payments` (new job, same shape as W1's `ap/qbo-push`):

1. Collect ledger rows with `status=Scheduled`, a `check_ref`, a
   `qbo_bill_id`, and no `qbo_payment_id`.
2. Group rows by `check_ref` — one check paying N bills becomes ONE
   `BillPayment` applying to N bills (live shapes: one check covered 2 bills,
   another covered 3; both become eval fixtures).
3. One approval card per run: "N payment records ready" with check
   numbers, vendors, amounts.
4. On approval: write each BillPayment (`DocNumber` = check number, txn
   date = scheduled payment date, `PrivateNote` = `engine:<key>` per W1
   provenance rule), read back, store `qbo_payment_id` on every covered
   row, emit `ap.qbo.payment_created`. Readback mismatch: anomaly, stop.
5. Idempotency: write keyed `qbo-payment:<check_ref>:<tenant>`; re-run
   after partial failure writes only checks lacking stored ids.

Rows without a `qbo_bill_id` (pre-W1 history, e.g. Arrow ACME20260101C/-2)
are out of scope: no bill to apply a payment to. They surface on the card
as "skipped: no QBO bill" so the owner sees the boundary. The imported-
history backfill decision from W1 stays NO by default.

Duplicate guard (W1 rule 2) applies: an existing QBO transaction with the
same vendor+amount in the ±14-day window parks a card instead of writing —
that is exactly the shape of the owner having already match-clicked or
hand-entered the payment.

## What W2 does NOT touch

- **Deposits / AR receipts.** Owner-coded (he holds the customer portal's
  context); Bianca-2 territory later.
- **Owner-reimbursement Zelles.** The expenses agent's report-matcher proposes those
  splits (see `expenses-design.md` §1); they are never AP and never W2's.
- **Bank-feed acceptance.** No API exists; W2's whole point is to make
  the remaining click a trivial pre-matched confirm.
- **Money movement.** Never (invariant 7).

## Evals (before implementation, invariant 8)

- One check, many bills: a three-bill fixture produces ONE BillPayment
  applying 3 bills, all three rows get the same `qbo_payment_id`.
- No bill id -> skip + card note, zero writes (a pre-W1 vendor fixture).
- Duplicate guard: existing same-vendor/amount txn -> card, no write.
- Rule 3 holds: the engine-written BillPayment appearing in QBO evidence
  must not flip its own rows (extends the existing independence eval).
- CSV clearing path: CSV line matching a `Scheduled` row with an engine
  payment record flips it Paid and emits the payment-details event;
  absent CSV, the row stays Scheduled indefinitely (no time-based flip).
- Approval gate: no approval, no write, including re-run.

## Rollback

Per W1 contract: batch rollback = scripted delete of the written
BillPayments by stored id, `ap.qbo.payment_deleted` events record it,
ledger history untouched.

## What shipped (2026-09-12, row 7.2)

`ap/qbo-push-payments`, the job above, with these refinements (each with
an eval in `core/agents/ap/evals/test_qbo_push_payments.py`):

- **Scope:** committed rows (`Scheduled`, `Scheduled in bill pay`) with a
  non-empty `check_ref`, a `qbo_bill_id`, and no `qbo_payment_id`, grouped
  by the normalized check reference. A check is recorded whole or not at
  all: one row without a QBO bill skips its whole check (listed on the
  card with the reason); a reference naming two vendors skips too.
- **The card** (`ap.qbo_payment_batch`) carries a structured `checks` list
  (check, vendor, row ids, bill ids, amount, payment date, engine key) and
  a `skipped` list; execution reads the structure, never display text
  (the 7.1 shape). An approval covers exactly the rows and amount the
  owner saw; a check whose scope moved parks afresh. The queue refuses
  a card with nothing left to write (`APPROVAL_CHECKS`).
- **The write:** `PayType Check`, `CheckPayment.BankAccountRef` = the
  tenant's `[close].bank_account` resolved through the chart (unmapped
  parks `ap.qbo_map_account`, nothing writes), `TxnDate` = the row's
  payment date (else the day it was marked Scheduled, else today),
  `DocNumber` = the check number, `PrivateNote` = `engine:qbo-payment:
  <check>:<tenant>`, one Line per bill with a `LinkedTxn` to the Bill.
- **Record-then-call-then-record** (honesty audit 03-F9): a
  `ap.qbo.payment_write.started` job record before the create call, a
  `ap.qbo.payment_write.done` record (with the id) the instant it returns
  and before the readback or the row update. The next run heals from the
  done record (rows never got the id) or from the `engine:<key>` note the
  accounting system holds (the call landed, the record did not); a key
  found on a different vendor or amount is a reused check number and is
  refused (`ap.qbo.payment_key_collision`). A started record with nothing
  in the accounting system is retried, named by
  `ap.qbo.payment_write_retried`.
- **Readback** compares amount, vendor, and the set of linked bills; a
  disagreement is `ap.qbo.payment_readback_mismatch` (a transport failure
  `ap.qbo.payment_readback_failed`), the id stays on every covered row,
  the event says `verified: false`, the batch stops.
- **The stored id** is the evidence form, `BillPayment:<Id>`, so the read
  side's engine-authored filter (rule 3) drops it by construction; the
  event `ap.qbo.payment_created` carries both the raw `qbo_payment_id`
  and that `qbo_id`.
- **The owner's side:** `engine status <tenant> <ref> --scheduled --check
  <n> [--date YYYY-MM-DD]` records the instrument at scheduling time (the
  design assumed it; nothing recorded a check reference before clearing).
- **Not built here:** the CSV clearing path (row 7.3), rollback scripting,
  and wiring into the daily script (with the flag off the job is a noop
  line; row 7.3 owns `scripts/engine-ap-daily.sh`).


## Row 7.3: the statement tier (2026-09-12, issue #212)

`ap/reconcile` gains a second evidence source, the bank's own statement
file, read through the config-driven CSV adapter (`[bank_csv]`), with an
eval per rule in `core/agents/ap/evals/test_reconcile_statement.py`:

- **The file.** `--param bank_csv=<path>`. The daily script resolves the
  NEWEST `*.csv` in the tenant's `[bank_csv].statement_dir` and passes it;
  no folder, no export, or a path that is not there skips the tier with
  one log line and no anomaly. A malformed file (wrong header) fails the
  run loudly: the owner dropped it on purpose. The run key declares the
  file by content, so a re-export with a new line re-fires and the same
  bytes re-touched replay.
- **Scope.** Money-out lines carrying a check number (the bank's sign:
  negative). Deposits, ACH, and card lines carry bank text, not a payee the
  ledger knows, and no instrument the engine records; they are never
  unknown money here (the bank rules and the expenses lane own them).
- **Matching, instrument only.** Normalized check reference names the open
  rows; the cleared amount must equal a candidate group's sum; the clearing
  must fall inside the entry-slack / deposit-lag window (5 days before, 21
  after) around the group's recorded payment date. Candidate groups: each
  engine-recorded payment (rows sharing a `qbo_payment_id`), rows with no
  payment record by vendor, and the whole set under the reference when
  there is more than one. Exactly one fitting group settles every row in
  it: `Paid`, the statement's cleared date, the check reference,
  `ap.reconcile.paid` per row with `evidence: statement`, the statement
  identity, and `qbo_id` = the row's own BillPayment (the record the
  auditor's QBO lens can verify; empty when the row has none, which the
  lens skips), plus `ap.invoice.paid` per row with `resolved_by:
  statement`. Several fitting groups (a reused number) park one
  `ap.reconcile_review` card in the 7.1 shape (structured `payments`,
  `source: statement`); the owner's `--param row=` answer executes on the
  next run with the chosen row's payment record as the `qbo_id`. A
  reference that fits but an amount or date that does not parks review
  too. No candidate is `ap.reconcile.unknown`, once per line identity
  (date + cents + reference), keyed so a longer export never re-flags it.
- **One physical check, two sources.** A check QBO evidence already flagged
  unknown is not flagged again from the statement, and the reverse; the
  owner mutes it once. A check QBO evidence already settled is
  `already_recorded` on the statement.
- **The daily run.** `qbo-push-payments` runs between `qbo-push` and
  `reconcile` with its own exit code; reconcile takes the statement file
  when one exists. The first tenant's `[qbo].payment_records` is `true`; the demo's
  stays `false`.


## The bill-pay exception (2026-09-17, issue #285)

One deliberate exception to the rule above that the check number is the
discriminator, because one shape can never satisfy it: a check the BANK
writes. The owner schedules a bill-pay payment with a send date and no
number (the bank assigns it later), so the row sits committed with an empty
reference and the statement line that finally carries the number matches
nothing.

`[qbo].bill_pay_channels` names the registry `payment_channel` values whose
checks the bank writes. When a check line has no row under its number and
no settled row to backfill, one last tier runs: a committed row whose
vendor carries one of those channels, carrying no reference of its own,
whose amount equals the line and whose recorded payment date fits the
tier's usual window around the clearing (up to 21 days before it, at most 5
days after), settles and takes the number from the line. The window is the
asymmetric one on purpose: the date recorded on a bill-pay row is the day
the BANK SENDS the check, and the payee deposits it one to three weeks
later, so the deposit lag is the whole point; a clearing that predates the
send date beyond entry slack is a different payment. **Exactly one such row, or nothing happens:** two
candidates park the 7.1 review card, since nothing but a human can tell two
same-amount bill-pay rows apart once the bank owns the numbering.

Off by default (an empty list), so no tenant inherits it by upgrading, and
`--param bill_pay_channels=<a,b>` overrides the config for a one-off run.
The tier runs after the reference backfill, so recorded money explains the
line first. Evals: `core/agents/ap/evals/test_reconcile_bill_pay.py`.
Decision: `docs/decisions/2026-09-17-bill-pay-checks-settle-on-channel.md`.
