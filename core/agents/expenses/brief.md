# The expenses agent

The expenses agent owns the expense-report cycle end to end: receipts dropped in the shared drop folder become filed paper, an owner-reviewed report package, a QBO split
record the bank feed pre-matches, and a clearing confirmation from bank-CSV
evidence. Design of record: `docs/expenses-design.md` (approved 2026-08-04).

## Jobs

- **intake** — scan the per-person drop tree (`[expenses].drop_dir`,
  `<person>/<project>/` subfolders). Attribution is deterministic: the
  project subfolder, else a filename project tag, else an attribution card.
  There is no guess path. Receipts file to
  `03_Expenses/<month>/receipts/<person>/<project>/`; a re-dropped or
  renamed duplicate noops on its content hash.
  A combined-scan pre-pass (issue #104) runs first: a receipt PDF with more
  than one page gets an LLM page-grouping proposal; CODE validates coverage
  (every page exactly once) and splits with pypdf into the same drop spot,
  children inheriting the original's attribution (tag normalized to
  underscore form) and flowing through this same run. The original archives
  to the drop tree's `_originals/` under an `expense.scan_split` lineage
  event (page map + child hashes). One group covering all pages = a
  multi-page single receipt, left whole under an `expense.scan_single`
  verdict. Splits run unattended — a file operation, no money, no external
  send — and a failed or invalid grouping HOLDS the scan (nothing filed,
  anomaly raised): a 17-receipt scan must never intake as one line. The
  proposed vendor/amount/date land only in child filenames, where extract's
  cross-checks treat them as claims, never accepted values.
  An archived original that comes back into the drop tree is held, never
  filed whole (the split verdict is the memory); an `_originals/` entry
  with no lineage event is an anomaly every run until a human clears it.
- **extract** — per landed receipt the extractor proposes vendor, date,
  amount, and category; deterministic cross-checks (amount-in-filename,
  project tag) flag disagreement. Proposals are events. Only owner-approved
  values ever enter the workbook or the ledger (invariant 2).
- **report** — on demand per person+month, plus survey mode that raises a
  draft-report card per person holding unconsumed receipts (month-end never
  waits on loose paper). On the approved review card: `expense_report` +
  `expense_line` rows land, the xlsx + receipts zip + manifest render, and
  consumed receipts move to `_filed/<report-key>/`. The manifest is what
  "consumed" means. A report left Open with no confirm card and no built
  event (a run that died after its rows committed) is recovered on the
  next run: card and event re-derived, the move finished, an anomaly says so.
- **match** — on the approved reimbursement confirm: the engine writes the
  QBO split Purchase (payee = the person, one line per category), duplicate-
  guarded, provenance-stamped, readback-verified — the feed line arrives
  pre-matched. Bank-CSV evidence (W2 Option B) flips Reimbursed; absent CSV
  the record holds Reimbursed-Recorded indefinitely. A rejected confirm card
  re-asks on a fresh key; the duplicate-review card is record-only (nothing
  executes on it) and counts as blocked, not parked; a safety-net fetch
  failure is an anomaly, never a silent empty scan.
- **inbox** — classify camera images in the receipt inbox, label-only, and
  propose: one filing card per receipt, one skip card for the night's
  not-receipts. Skips move only on approval, so an unanswered night's set
  rides into the next; the new skip card declares `supersedes_keys` for every
  older pending skip card whose files it fully contains (strict subset), and
  the runner closes those as `superseded`, never `approved`. Containment,
  never recency: a card holding a file tonight lacks keeps asking.

## Standing rules

- Owner reports are owner reimbursements, never AP. A vendor billing
  expenses through their own entity is true AP and the expenses agent never touches
  that paper — it stays entirely in the AP lane. A vendor who does NOT
  invoice — receipts in the drop tree, paid directly — is a person with
  `role = "vendor"` (issue #120): same intake/extract/report flow, but the
  Purchase books to the vendor record named by `qbo_vendor`, one line per
  PROJECT on the chart account `[expenses].project_account_template`
  resolves (project costs, no overhead accounts, the category table is
  never consulted), and the payments count toward the vendor's 1099 total
  through the year-end lens's ledger_aliases fold. An unresolvable project
  account parks a card, never a guess.
- A top-level drop-tree folder matching no configured person warns once
  (the recorded event is the memory, never a daily nag) and, when
  receipt-suffixed files sit inside, raises a review card (issue #119).
  Its files are never processed — the persons list stays the authority
  on treatment.
- Meals default to **Meals** (the 50% limitation is the CPA's year-end
  act). **Entertainment is never assumed** — it exists only when the owner
  tags it in the filename.
- The engine never moves money (invariant 7). The owner reimburses; the expenses agent
  records, approval-gated, and never writes without a card.
- Credit-card receipts are filed paper (`_cc_charges/`), not ledger rows;
  statement reconciliation is a later build.
