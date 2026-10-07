# Agent: ap

## Role

The AP agent owns inbound vendor paperwork: invoice intake from the tenant's
landing directory, payment verification, and the AP side of the approval
queue. It is the engine port of the legacy AP agent's charter, stripped of
tenant specifics (tenant identity, vendor names, thresholds, and paths all
live under `tenants/<slug>/`). It records and files invoices on its own
(`apply` copies the renamed original; no approval gate, since filing is not a
money move); the owner approves only the genuine judgment calls (a new vendor, a
revised amount). It never moves money, and every write passes the guard.
The owner sets payment status (scheduled, paid) through the `status` write-back,
which the forward-only transition rules validate; that status drives both the
workbook colors and the parity diff. Files the engine could not turn into an
invoice (failed extraction, image-only) surface in the workbook's "needs
identification" section; the owner resolves each with `dismiss` (not an invoice)
or `identify` (manual entry records the invoice). A resolved file never
resurfaces.

## Jobs

- `intake`: classify and extract every candidate document in the landing
  directory (classification and field extraction may use an LLM through the
  extraction boundary; everything downstream is deterministic code), resolve
  the vendor against the tenant registry, dedup against the engine ledger on
  vendor + invoice number, record NEW invoices, flag the rest. A flag whose
  cause is the pipe, not the document (`transport_error`, `timeout`), is
  retried on the next run and then once a day for as long as the file stays
  in the landing folder; `oversize` and `bad_reply` are verdicts about the
  file and stay final until the owner dismisses or identifies it. An inbound
  W-9 is caught DETERMINISTICALLY before the model (filename tokens, then
  PDF text markers — the form carries a TIN and no TIN ever reaches a
  model, card, event, or log): it routes with one approval card proposing
  the registry flip; the approved card's execution copies the form into
  the tenant's W-9 folder and emits the registry diff as an event — the
  engine never writes the vendor registry (w9-1099-design.md build 3).
  Every invoice that resolves to a vendor also gets a sender verdict
  (`ap.provenance.recorded`, issue #356): the mail fetch's saved record is
  joined by content hash, and the sender is judged against the vendor's
  domain-shaped registry keys, its exact `senders` addresses, and the
  tenant's `[ap.provenance]` internal, platform, and free-mail domains
  (`match`, `owner_drop`, `internal_forward`, `platform`, `mismatch`).
  Shadow stage: the verdict holds nothing and changes no invoice; the
  auditor's provenance lens surfaces the unbound ones.
- **Human-only cards.** `ap.new_vendor_decision` is in `HUMAN_ONLY`: the
  queue refuses to approve or reject it unless a person at a terminal types
  the card number back, and records `decided_via=terminal`. No lane decides
  it; the auditor flags one resolved any other way as CRITICAL.
- `verify-payment`: deterministic three-way verification (AP rows, cleared
  bank register, bill-pay queue snapshot). Engine code, never judgment
  (invariant 4). Emits payment recommendations into the approval queue.
- `queue-status`: read-only report of pending approvals and invoice statuses.
- `apply`: the execution step. Files every recorded invoice not yet filed by
  COPYING the landing original into the filing tree (a copy, never a move; the
  original is the audit artifact). No approval gate: a successfully-extracted
  invoice is real (junk is kept out upstream) and filing is not a money move.
  Shadow-aware (a shadow run is a dry-run that writes nothing) and guard-safe
  (the destination passes the write guard, so a `filing_dir` inside a protected
  surface is refused even live). Touches no money and no external accounting.
- `workbook`: render the human-readable AP ledger as a delivery view from the
  engine ledger (invariant 1). Column layout is tenant config (so the sheet
  matches the tenant's legacy layout while core stays generic); rows are colored
  by status group (blank payable, red committed, green paid, grey void). openpyxl
  with the tenant's legal name as author (invariant 10); the output path passes
  the write guard. A regenerated photograph of the ledger, never hand-edited.
- `qbo-push` (write side W1): record Bills in the tenant's accounting
  system for OPEN engine-recorded rows. Nothing writes without an approved
  batch card; every write is provenance-stamped, checked against existing
  transactions first (a lookalike parks for review, never writes), read
  back and verified, and its id stored on the row so a re-run can never
  double-push. Unmapped vendors or chart accounts park; the engine never
  guesses a mapping. Settled rows and imported history are out of scope
  (their money is already in the books; a Bill would double the expense).
- `qbo-push-payments` (write side W2, phase 7 row 7.2): record a
  BillPayment in the tenant's accounting system for every committed row
  that wears a check reference and a QBO bill, so the bank-feed line
  arrives pre-matched. One check paying N bills is ONE BillPayment
  applying to N bills (the check number as DocNumber, `engine:<key>` in
  PrivateNote). Off unless `[qbo].payment_records` is true (a tenant
  setting, off by default; with it off the job is one log line; on, it
  runs in the daily sequence between `qbo-push` and `reconcile`). Nothing
  writes without an approved batch card; a row with no QBO bill is listed
  on the card as skipped and never written; a same-vendor, same-amount
  record inside 14 days parks a duplicate-review card instead of writing;
  every write is remembered before the call and again the instant it
  returns (the job-records table), read back (amount, vendor, linked
  bills), and its id stored on every covered row, so a re-run never
  writes a check twice and a run that died mid-write is healed from its
  own record or from the key the accounting system holds. This job NEVER
  settles a row: Paid comes from clearing evidence (reconcile, the
  statement file), and reconcile never reads an engine-written payment as
  evidence. The owner records the instrument at scheduling time with
  `engine status <tenant> <ref> --scheduled --check <n> [--date <iso>]`.
- `reconcile`: read cleared money-out transactions from the tenant's
  accounting system (QBO receives the bank's direct feed) and settle the
  ledger's committed rows against them. Deterministic engine code end to end
  (invariant 4): one clean match flips to Paid with the cleared date and
  check reference; a check whose referenced group of rows sums to the cleared
  amount settles the group; any ambiguity parks an `ap.reconcile_review`
  card and flips nothing. The owner answers that card with `queue approve
  --param row=<id>`: the queue refuses an approval that names no candidate,
  a settled row, or a row whose amount disagrees with the cleared total
  (amount confirms, never discriminates), and the next run flips the chosen
  row exactly as a clean match would, attributed to the owner, with
  `ap.reconcile.paid` per payment and one `ap.invoice.paid` for the row
  (phase 7 row 7.1). A cleared payment no open row explains raises an
  `ap.reconcile.unknown_payment` anomaly (the 2026-05-19 orphan-check and
  2026-05-21 double-payment classes) and NEVER guesses a row. It creates one
  only where the owner approved a hand-check card naming the payee and the
  project (the lane below), never on its own reading of the evidence. A
  payable-status row that clears anyway settles but flags the status lag.
  Read-only toward the accounting system; the engine never writes to it from
  this job. Shadow mode reports would-settle lines and mutates nothing.
  The bank's own statements are the second evidence source (phase 7 rows 7.3
  and 7.4, `--param statement_dir=<folder>`, the tenant's
  `[bank_csv].statement_dir` when the daily run passes it; `--param
  bank_csv=<path>` still names one file): a money-out check line matching
  open rows on check number, amount, and a date window around the recorded
  payment date settles them with `evidence=statement` and the statement's
  cleared date, which is the ONLY way a payment the engine recorded itself
  (`qbo-push-payments`) ever settles, since its own record is never
  evidence. Several fitting groups park the same review card; a check line
  no row explains is unknown money exactly once per line and feeds the
  hand-check lane below; lines without a check number are out of scope; no
  folder means the tier is skipped with one log line.
  **The whole folder is read every run**, PDFs (the monthly statement, which
  arrives with no owner effort) and CSVs (an export somebody chose to make)
  alike, because a statement line's identity is its posted date, signed
  cents, and reference, so re-reading the same months settles nothing twice
  and asks nothing twice. All three sections are parsed and must tie to the
  counts and totals the bank printed, or the file is an
  `ap.reconcile.statement_unparsed` anomaly rather than a silent half-read;
  the withdrawals and deposits are not evidence today and land in one
  informational `ap.reconcile.statement_lines` event per file. A file that
  is not a statement at all is one log line. A folder that cannot be LISTED
  is an `ap.reconcile.statement_unreadable` anomaly under a key that never
  replays — "I could not look" is never "nothing to look at" (2026-09-13).
  One tier restores rather than settles: a check line with no open row under
  its number whose amount and date fit exactly one SETTLED row carrying no
  reference writes the number onto that row (`ap.reconcile.ref_backfilled`)
  and counts as already recorded. **Amount and date never settle an open
  row** — the check number is the discriminator for settling.
- `reconcile`, the hand-check lane (phase 7 row 7.4, `[qbo].direct_payment_cards`,
  OFF by default): some businesses pay contractors by hand check, and that is
  permanent normal operation rather than a deviation, so the engine treats it
  as a first-class path instead of an anomaly to nag about. A cleared payment
  no ledger row explains, whose coding says contractor work, parks an
  `ap.record_direct_payment` card proposing the payable; the owner approves
  with `--param project=` (and `--param payee=` when the feed named nobody),
  and the next run creates the Paid row. **The no-guess rule is unchanged**:
  nothing is created without that approval, and the queue refuses a card
  whose payee or project cannot be known rather than inventing either.
  THE DISCRIMINATOR IS THE REGISTRY'S `cost_type`, not the account: freight
  (excluded from 1099-NEC) and subcontract labor can code to the same
  project-expense account, so a registered payee cards only on a 1099-shaped
  cost type, while an unregistered or payee-less payment falls back to
  account patterns. A decided card, approved OR rejected, answers its
  clearing permanently and never re-asks. Why it exists: the CPA appendix and
  the vendor-1099 lens both start downstream of a ledger row, so a contractor
  paid direct from checking is invisible to both and silently absent from the
  January packet. The floor (`[qbo].direct_payment_floor_cents`, default the
  $600 reporting threshold) throttles first-pass volume and is explicitly not
  a tax rule, since the obligation aggregates across a year. Recording a
  payment the owner already made is not making one: no money moves here and
  invariant 7 is untouched. **The bank statement is the lane's second
  source** (row 7.4, 2026-09-16): a cleared check no ledger row explains
  parks the same card, with no payee and no coding to read — the bank knows
  the instrument, not the accounting — so the owner supplies both. The same
  physical check never cards twice across the two sources. Identity is the
  check number plus the amount wherever both sides carry a number. Where only
  one side does, and the bank always does, a second and narrower identity
  applies (#305): the same cents AND the same cleared date, exactly or inside
  the reconcile date slack, AND that number on no other feed evidence in the
  window. **Amount alone is never the discriminator**, and the pair has to be
  unique from both sides: two candidates means the lane pairs nothing and
  each side asks its own question. With the feed card in hand, whatever its
  status, the statement line stays silent; when the bank asked first, the
  richer feed card still parks, names the statement card in its reason, and
  executing it marks that card decided (rejected, with a note naming the card
  that answered it), which is the one place the engine decides a card the
  owner did not.
- `sweep-cards` (phase 7 row 7.4, `[qbo_sweep].parked_cards`, on where a
  sweep exists): the third source, and the only one that starts from a
  document a session wrote. A tenant whose bank feed is swept weekly gets a
  dated note listing, under a "Needs <owner>" heading, every feed row the
  sweep's policy could not click, each with a proposal. This job reads the
  NEWEST such note and parks one `qbo.sweep_parked` card per row, so the
  answer lands in the queue instead of waiting in the note for a week. **The
  card's key is the feed line, never the prose**: the transaction date, the
  amount in cents, the direction, and the bank's own text, none of which is
  the writer's choice, so the same row re-listed next week is the same
  question. Approving names the answer (`--param account=`, or
  `--param match=` when the row matches an existing record) and the desk
  script carries it into the next sweep session under "Post these"; a
  decided card, approved or rejected, never re-asks; a row naming a check
  the hand-check lane already asked about does not card again. The job
  reads a file and parks questions: it clicks nothing, writes nothing to the
  accounting system, and posting an answer stays inside the sweep session's
  own grant.

## Judgment guidance

The only judgment step is document classification and field extraction, and
its output passes pydantic validation before any code consumes it. Do not
guess an amount that is not printed in the document. A purchase order is a
PO even when it mentions invoicing; a vendor proposal or quote is never an
invoice; a photo or screenshot is reference material. When extraction cannot
produce a vendor, an invoice number, and an amount, the document is flagged
for review, not guessed at.

## Escalation rules

Escalate to the owner (via the approval queue) when:

- a document's vendor is not in the tenant registry (onboarding is an owner
  decision; the registry is never auto-written),
- an invoice's amount differs from the recorded row (REVISED is reviewed,
  never auto-overwritten),
- extraction is incomplete or the document is image-only (needs OCR),
- any item verifies as payable (payment recommendations queue; nothing pays
  automatically, and in shadow phases nothing pays at all).

The agent must never: write the vendor registry, mark a payment executed,
write a protected production surface (the guard refuses it; `apply` files only
to the permitted `filing_dir`), delete a landing original or move one out of
the landing tree (originals are the audit artifact; the `janitor` job may
organize aged originals into `_archive/YYYY-MM` WITHIN the tree, owner
decision 2026-07-09), or act on anything external while shadowing.

## Input / output contract

Input: `schema.ExtractedDocument` (the validated LLM boundary), the tenant's
`vendors.toml`, an optional bank CSV (`adapters.bank_csv`), and an optional
bill-pay queue snapshot (`schema.BillpayEntry` list). Output: the standard
`RunResult`; AP rows land in `ap_invoices` with one `ap_status_history` row
per status flip.

**The delivered workbook stays current (#326, 2026-10-04).** Every committed
AP write (the store's insert, status flip, payment details and notes, a hand
backfill included) marks the view stale; the end of any engine invocation,
and the 15-minute retries job, renders it once through the unchanged
`workbook` job. A failed render is an anomaly and never touches the AP write;
shadow never renders; the 08:00 line stays as the backstop and for view
version changes.
