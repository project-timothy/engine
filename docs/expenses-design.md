# The expenses agent: design

Status: **DRAFT for the gate day** that scopes the expenses agent alongside W2
(`w2-billpayment-design.md`; owner slotted both 2026-07-23). Merging the PR
approves the design; code follows in TDD'd PRs after the July close. The four
owner decisions below were made in the 2026-07-27 design session; the design
inputs captured 7/23–7/24 (#81, #82) are folded in and superseded by this file.

## The loop the expenses agent closes

The cycle, as it runs by hand before the agent:

1. Receipts land in `03_Expenses/<month>/receipts/<person> <project>/`.
2. A session built `reports/<person>_EXP-<month>/<Tenant>_Expense_Report_<month>.xlsx`
   (all lines project-coded) + receipts zip.
3. The owner reimburses the report total from the business account.
4. The payment cleared in the QBO bank feed and was split-coded by hand
   (rental car / lodging / meals / fuel, from the report lines).

The expenses agent owns steps 1–4 end to end: intake the receipts, build the report,
record the reimbursement in QBO so the feed line arrives pre-matched (the same
record-exists-before-the-feed-line principle W2 established), and confirm the
clearing from bank-CSV evidence.

## Owner decisions (2026-07-27 design session)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Split coding delivery | **Engine writes the split expense record in QBO**, approval-gated, reusing W2's write machinery and invariants. Card-only was rejected: it leaves the hand-coding step in place every month. |
| 2 | Project attribution at the drop | **Project subfolder**: `Expenses/<person>/<project>/` in the shared drop folder. Filename tags (`P2035`) are the fallback; a receipt with neither gets a card, never a guess. |
| 3 | Credit-card receipts | **File only, recon later.** The mail agent routes recognized cc receipts to `_cc_charges/` instead of tripping `mail/not-landed` nightly (a small software-subscription charge was the first case). Statement reconciliation is a later build. |
| 4 | Report cadence | **On demand + month-end.** "Do my expenses" works any day; unconsumed receipts at month-end raise a draft-report card automatically so the close never waits on loose paper. |

## Agent shape

`core/agents/expenses/`: `brief.md` + `schema.py` + `jobs.py` + `evals/`,
the timesheets-vertical pattern. Four jobs.

### 1. `expenses/intake` — drop folders to filed receipts (daily run)

- Scan the tenant's per-person drop folders in the shared drop folder
  (`expense_drop_dir` in tenant.toml, one subtree per person; persons and
  their reimbursement class — owner vs employee — are tenant config).
- A file is a receipt if its suffix is in the receipt set (the closer's
  `_RECEIPT_SUFFIXES`, single shared definition).
- Project attribution, in order: containing subfolder name, then a filename
  project tag, else an attribution card (decision 2). No guess path exists.
- File to `03_Expenses/<month>/receipts/<person>/<project>/` (replaces the
  July one-off `<person> <project>` flat folder; the layout mirrors the drop
  tree). Month = receipt drop month; extraction may re-home a receipt whose
  parsed date says otherwise, as a card note, never silently.
- Idempotency key `expense-receipt:<sha256>` — a re-dropped or renamed
  duplicate noops with a card note. Ledger event `expense.receipt_landed`.
  The intake summary counts `already-filed` (a filed receipt left in the
  drop tree) apart from `duplicate` (the renamed re-drop with its card
  note). An archived combined-scan original that comes back into the drop
  tree is HELD with `expenses.split_original_redropped` every run it sits
  there, never filed whole: the `expense.scan_split` verdict is the memory
  (honesty audit 2026-09-03). An `_originals/` entry with no lineage event,
  or a moved inbox file with no `expense.inbox_filed` record, is an anomaly
  on the next run, never silence; the inbox case is recorded once the file
  is found where its card said, hash-verified.

### 2. `expenses/extract` — LLM proposes, the owner confirms

- Per receipt, the LLM proposes: vendor, date, amount, category. Deterministic
  cross-checks run first and disagreement flags: amount-in-filename
  (`$12.34`), project tag vs attributed project.
- Category rules (owner, 2026-07-22, standing): every meal line defaults to
  **Meals** (the 50%-limitation category at year end; the limitation is the
  CPA's act, never applied at reimbursement time). **Entertainment is never
  assumed** — it exists only when the owner explicitly tags it (the 0% /
  nondeductible case). Meals billed through to a client as separately stated
  reimbursables shift the limitation to the client; per-receipt project
  attribution preserves that option.
- Invariant 2 boundary: proposals surface on the report review card;
  **only owner-approved values enter the workbook or the ledger.** The LLM
  never writes a money value anywhere directly.

### 3. `expenses/report` — the package (on demand + month-end, decision 4)

- Build from approved rows only: `<Tenant>_Expense_Report_<YYYY-MM>.xlsx`
  (openpyxl, metadata = tenant legal name, invariant 10, hardcoded-values
  recipe) + `<Tenant>_Receipts_<YYYY-MM>.zip`, into
  `reports/<person_id>/` as today.
- Write a **manifest** (report id, receipt filenames + sha256s, line values)
  next to the package; move consumed receipts to `_filed/<report-id>/`
  (ratifies the May precedent). The manifest is what "consumed" means.
- Ledger: `expense_report` row (person, month, total, status **Open**) +
  `expense_line` rows. **Schema addition = one-way door**, flagged here for
  the gate: two new tables, no changes to existing tables.
- Treatment split (standing, 2026-07-11): owner reports are **owner
  reimbursements, never AP**. Employee reports (meals reimbursed at 100%,
  the only current case) follow the same report flow. SUPERSEDED IN PART
  2026-08-14 (issue #120): a contractor who does not invoice through his LLC
  submits receipts through the drop tree as a `role = "vendor"` person.
  Same report flow; the match books to his LLC's vendor record, one
  line per project on the house COGS subaccount (project costs, never
  overhead — owner directive 2026-08-17), and the payments ride his
  1099-NEC total via the year-end lens's ledger_aliases fold. A vendor who
  DOES invoice through their entity remains true AP and stays out of the expenses agent.
- Month-end: if unconsumed receipts exist for the closing month, raise one
  draft-report card per person. Close preflight check 8 (below) is the
  backstop, this card is the fix-it-forward.
- Rows commit before the receipts move (#135). A run that dies between the
  two leaves the report Open with no confirm card and no
  `expense.report_built` event; the next `report` run (either mode)
  re-derives both from the Open row, finishes the `_filed/` move, and says
  so (`expenses.report_recovered`), independent of unconsumed receipts.

### 4. `expenses/match` — reimbursement recorded, feed line pre-matched

- Primary path (the July live shape): the report card carries a
  "reimbursed via <channel> on <date>" confirm. On it, the engine writes the
  QBO split expense record (Purchase): payee = the person, one line per
  report category with the approved amounts, `PrivateNote = engine:<key>`
  (W1 provenance rule), readback verify, idempotency
  `qbo-expense:<report_id>`. Report flips **Reimbursed-Recorded**. The feed
  line then arrives as a one-click match.
- Safety net: the match job scans clearing evidence for payments matching an
  **Open** report (payee in the tenant owner/person payee list — the same
  list reconcile's ignore rule reads — exact amount, date window from tenant
  config). A hit on an unconfirmed report raises the same card; it never
  writes unprompted. Two open reports matching one payment = card, no
  auto-pick.
- W1 rule 2 duplicate guard applies: an existing QBO transaction with the
  same payee+amount in the ±14-day window parks a card instead of writing —
  exactly the shape of the owner having already hand-coded it. That card
  (`expenses.qbo_duplicate_review`) is RECORD-ONLY: nothing consumes its
  resolution, approving or rejecting it executes nothing, and the report
  stays Open until a human reconciles the existing record by hand; its
  reason text says so and the match summary counts it as `blocked`, never
  `parked`. The confirm card (`expenses.reimbursement_record`) follows the
  close pattern instead: a rejected card followed by a fresh ask re-parks
  on `expconfirm:{report_id}:reask-{card_id}`.
- The match summary reads `recorded R, parked P, blocked B, cleared C`:
  parked is a card the owner can clear (payee, project, or category
  mapping), blocked is an anomaly that parked nothing (bank account
  unmapped, the create rejected, the duplicate guard). The leg-2 safety net
  stays best-effort, and a failed evidence fetch is
  `expenses.safety_net_unavailable` on the run, never a silent empty scan.
- Clearing: per W2's Option B, the bank CSV is the clearing evidence for
  engine-recorded transactions (rule 3: the engine never reads its own
  records as evidence). CSV hit flips the report **Reimbursed**. No
  time-based flips; absent CSV, Reimbursed-Recorded holds indefinitely.

## Mail-agent addition (decision 3)

A recognized credit-card charge receipt (sender/subject rules in tenant
config, seeded with the live repeat offenders) routes to
`03_Expenses/<month>/_cc_charges/` with a card note instead of flagging
`mail/not-landed` every night. No extraction, no ledger money rows — filed
paper awaiting the statement-recon build. Medical-sender filtering is
untouched.

## Close-preflight check 8 upgrade

`check_expenses` today counts every receipt-suffix file under the month and
cannot tell consumed from loose (it warned on July after the report was
filed). Upgrade: a receipt is **consumed** if it appears in a report manifest
or lives under `_filed/`; `_cc_charges/` and `_reference/` are excluded as
filed-not-loose. The WARN fires only on genuinely unfiled paper. Stays
advisory-only, never blocks (the legacy close script's rule, kept).

## What the expenses agent does NOT touch

- **Invoiced vendor paper** — true AP, existing AP lane (2026-07-10
  lesson: a five-figure invoice nearly buried; never dismissed). The
  vendor-role contractor's receipts moved INTO the expenses agent on the 2026-08-14 decision (issue
  #120) — he does not invoice; the vendor-role match books his payments to
  his LLC's vendor record instead of an owner reimbursement.
- **Money movement.** The owner Zelles himself; the engine records
  (invariant 7).
- **Credit-card statement reconciliation** — later build (decision 3).
- **W2's BillPayments** — vendor checks stay W2's; owner reimbursements stay
  the expenses agent's. The two writes share machinery, never rows.
- **Deposits / AR** — Bianca-2 territory.

## Evals (before implementation, invariant 8)

- Duplicate drop: same receipt re-dropped (renamed) noops on the hash key.
- No-attribution receipt → card, nothing filed to a project, no guess.
- Filename amount disagrees with LLM proposal → flagged, not silently chosen.
- Meals default: an untagged restaurant receipt proposes Meals;
  Entertainment appears only via explicit owner tag (July fixture).
- Manifest consumption: July-shaped fixture (filed report + one loose
  receipt) — check 8 warns only on the loose one; `_cc_charges` excluded.
- Approval gate: no card approval → no workbook values, no QBO write,
  including re-run (invariant 7).
- One payment, two open reports at the same amount → card, no auto-match.
- Duplicate guard: owner already hand-coded the Zelle → card, zero writes.
- Readback failure or mismatch after the QBO write → anomaly, stop; the
  Purchase id is already on the report (status stays Open: unverified),
  the next day's match run reads it back and completes the record
  (`expense.qbo_purchase_verified`); never a second Purchase, and the
  unverified id is still the engine's own writing under rule 3.
- Rule 3 independence: the engine-written Purchase appearing in QBO evidence
  must not flip its own report (extends the existing independence eval).
- CC receipt route: card-receipt mail files to `_cc_charges/`, no
  `mail/not-landed` flag, card note emitted.

## Rollback

Per the W1 contract: scripted delete of written Purchases by stored id,
`expense.qbo_purchase_deleted` events record it, ledger history untouched.
Filed receipts and reports are moves + manifests, all reversible from the
event log.

## Owner price directive (2026-09-11)

`expenses report --param person=<name> --param "price:<sha16>=amount:category[:note]"`
prices a landed receipt the extractor could not read, or corrects one it
read wrong. Recorded as `expense.owner_priced`, overlaid on the proposal
on every later run (the approval run carries no param). Two receipts on
one scanned page: price the page, then `split:` it at approval. Decision:
`docs/decisions/2026-09-11-owner-price-directive.md`.
