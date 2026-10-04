# QBO write side (Build 3) — design

Status: DRAFT for owner review, 2026-07-16. Merging this PR approves the
design; code follows in separate TDD'd PRs, one phase per PR.

## Goal

The engine writes the tenant's accounting records instead of the owner
clicking through QBO. Tonight's version of this was manual: the owner
posted a $5,255 five-invoice check batch by hand. After Build 3, the
engine does that entry, coded from ledger data it already owns, gated by
the approval queue.

The engine still never moves money (invariant 7). It writes *records*.

## The one hard problem: double-entry

Three writers can now touch the books: the engine, the owner, and the
bank-rules automation. If two of them record the same economic event, the
P&L is wrong until someone notices. Every design choice below exists to
make duplicate entry structurally impossible, not merely unlikely:

1. **Provenance stamp.** Every engine-created QBO transaction carries
   `engine:<ledger-idempotency-key>` in `PrivateNote`, and the returned
   QBO id is stored on the ledger row. Engine authorship is always
   detectable from either side.
2. **Query-before-create.** Before writing, the engine searches QBO for
   any transaction with the same vendor and amount inside a ±14-day
   window. A hit means a human or rule may already have recorded it:
   park an approval card, never auto-write. (This is what would have
   caught tonight's $5,255 had both writers been active.)
3. **Reconcile ignores engine-authored records.** The read side treats
   QBO postings as cleared-payment evidence. An engine-written record is
   NOT evidence — the engine must never launder its own writing into
   proof that money cleared. Stored ids from (1) are filtered out of the
   evidence stream.
4. **Who-writes-what contract.** The engine writes Bills for
   engine-recorded invoices, and (phase W2) payment records for
   ledger-known payments. Humans and bank rules own everything else.
   Any collision falls into (2) and parks.

## Phases

### W1 — Bills (this build)

For every ledger invoice row without a stored QBO bill id, create a QBO
`Bill`: vendor, invoice number as `DocNumber`, amount, invoice/due dates,
line coded to the row's `gl_account`. Fills the workbook's QBO_Bill_ID
column, gives QBO real AP aging and vendor balances, and makes the bank
feed's match suggestions meaningful for vendor checks.

Zero bank interaction. A wrong Bill is deletable. Lowest possible blast
radius; proves the whole write path (auth, mapping, approval, readback).

Flow, per daily run:

1. `ap/qbo-push` job collects rows in scope (see below) lacking a bill id.
2. One approval card: "N bills ready to record in QBO" with the list.
3. On approval, write each Bill; store the QBO id on the row the instant
   create returns; then read it back and emit `ap.qbo.bill_created` with
   `verified` true or false. A readback that raises or disagrees is an
   anomaly (`ap.qbo.readback_failed` / `ap.qbo.readback_mismatch`) and
   stops the batch, but the id stays on the row: the Bill exists whatever
   the readback said, and a forgotten id is a second Bill next run
   (honesty audit 2026-09-03, 02-F1). The nightly qbo lens verifies the
   Bill externally.
4. Idempotency: job key includes the set of unpushed rows; each write is
   keyed `qbo-bill:<invoice idempotency key>`; a re-run after partial
   failure pushes only what lacks a stored id.

Scope for W1: rows recorded by the engine on/after the cutover
(2026-07-09) that are not settled-by-import history. Backfilling the
imported year is a separate owner decision (it would double QBO's
existing expense records for anything the owner already entered —
default is NO backfill).

Trust gate: approval card per batch for the first two weeks minimum;
after that the owner may lower the gate to auto-push with a daily
summary, his call, one config value.

### W2 — Payment records (next build, needs one owner decision)

Goal: when the ledger knows a payment (owner schedules with a check
number, or a clearing is confirmed), the engine records the matching
`BillPayment`/check in QBO so the bank feed line auto-matches instead of
sitting uncoded in For Review.

The evidence problem: the QBO API cannot see the bank feed's For Review
queue, and it cannot see whether a posted record has been matched to a
feed line. If the engine posts payment records at scheduling time, the
read side loses its clearing signal for those payments (a record now
exists before the money cleared, and reconcile must not read it as
cleared — rule 3 above — but nothing else in the API will ever say
"cleared" either).

So W2 requires choosing the clearing-evidence source:

- **Option A — feed-acceptance stays the signal (default).** The engine
  writes Bills only (W1); payment records keep entering QBO through the
  owner's match-click or bank rules, exactly as today. Cost: one click
  per vendor-check clearing, made trivial by W1's bills. The ledger's
  red/green freshness stays coupled to how often those clicks happen.
- **Option B — bank CSV becomes the clearing signal.** The owner exports
  the bank's CSV when freshness matters (~2 minutes, on demand);
  the engine reads TRUE cleared data from it, flips ledger rows Paid,
  and THEN writes the payment records to QBO — post-clearing, so rule 3
  never bites, and the feed lines match engine records afterward. Cost:
  an occasional manual export. Benefit: ledger colors and QBO books stay
  current without per-transaction clicks.

Recommendation: A now (it is free once W1 lands), revisit B if the
match-click cadence annoys the owner in practice. The CSV adapter
already exists either way.

## Schema change (stop-and-flag item)

`ap_invoices` gains two nullable TEXT columns via migration:

- `qbo_bill_id` — id of the engine-created Bill (W1)
- `qbo_payment_id` — id of the engine-created payment record (W2)

Forward-only migration, no existing data touched. Merging this design
approves the migration.

## Mapping (deterministic, no LLM)

- **Vendor:** ledger vendor name -> QBO Vendor by display-name match
  through the vendor registry's canonicalization (aliases included).
  No match -> approval card offering create-vendor (creation is itself a
  write, same gate). Never fuzzy-guess across distinct QBO vendors.
- **Account:** the row's `gl_account` string -> QBO Account by
  fully-qualified name (e.g. `Cost of Goods Sold:Project Expense - RFC
  1017`). These strings came FROM the tenant's QBO chart originally, so
  mismatches mean the chart changed: anomaly, card, no write.
- **Amounts:** integer cents -> QBO decimal via Decimal. No floats.

## Rollback

Every write is an event carrying the QBO id. Rollback of any batch =
delete those QBO transactions by id (scripted), ledger rows keep their
history (`ap.qbo.bill_deleted` events record the reversal). The ledger
is never the thing rolled back; it is the record OF the rollback.

## Evals (before implementation, invariant 8 / TDD)

- Duplicate guard: a similar existing QBO txn (same vendor+amount in
  window) -> approval card, zero writes.
- Idempotent re-run: partial-failure retry pushes only unbilled rows;
  no row ever produces two Bills.
- Readback failure or mismatch -> anomaly, batch stops, the id is on the row
  (unverified), never a second Bill for the row.
- Unmapped vendor / unmapped account -> card, no write, no guessing.
- Reconcile ignores engine-authored records: an engine-written payment
  record in the evidence window must not flip a Scheduled row.
- Approval gate: no approval, no write, including on re-run.

## Out of scope

Bill *payments* through QBO (money movement — never), AR/invoicing (own
phase), backfill of imported history (separate owner decision), payroll
entries (the payroll provider's own sync owns them), bank-feed acceptance
automation (no API exists; rules + W1 matching are the answer).
