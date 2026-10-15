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
- **month** — the missionary's month (alpha step 3, 2026-10-13): every
  person holding receipts for the month (`--param month=YYYY-MM`, else the
  month that just ended on the tenant's clock) gets one report and one
  review card, the same build the report job does per person, so the
  reviewer's yes is the only step left; an approved card builds its report
  on the next run. A newer card for the same person and month retires the
  older one still waiting (superseded, never approved). A review card names
  its receipts (`receipts`) and its yes is to them (records.py, 2026-10-14):
  a late receipt waits on a small card of its own that says what is
  already approved (`already`), and the month builds once every receipt in
  it holds a yes. A yes holds while the receipt's facts hold: a reading,
  amount, price or category recorded after it puts the receipt back on a
  card. `records.where` is the one reader of where a receipt stands
  (captured, read, said, placed, approved, reported, sent, paid back). A door receipt
  (`<day>_receipt_<claim>`, core/tools/receipts.py) is priced from the
  person's own words: a dollar claim when nothing else priced it; a claim
  in another currency ALWAYS at the rate on the receipt's own date
  (`fx.py`: the free currency-api feed, no key, a mirror behind it,
  `ENGINE_FX_FEED` to point it elsewhere), recorded once as an
  `expense.fx_rate` event keyed by currency and day, so a rate never moves
  after it is set. The line keeps the amount and currency as said, the
  rate, its date, and its source (migration 9). No rate for the day = the
  line waits ("rate pending") and no card is raised with a guessed number;
  the next run asks again. A currency word ("rupees") is settled by the
  person's own zone when the photo lands (Kathmandu = NPR); unsettled, it
  stays unpriced for the reviewer to see. Code does every step; no model.
- **categorize** — place one receipt in one of the ministry's categories
  (`[expenses.category_accounts]`; the owner's decisions of 2026-10-13).
  Called by the door: when a photo lands, the person's own words place it
  (`categories.from_words`, code: "the bus to Pokhara" is Travel; words
  that point nowhere or two ways leave it uncategorized), and later the
  reviewer, or the person on their own receipt, moves it by asking their AI
  (`set_category`). A category moves no money, so the word decides: the
  change is an `expense.category_set` event naming who said it and their
  words, the latest one wins, and a month already waiting for its yes is
  raised again showing it (the older card superseded). Every nonprofit
  shape starts from one starter list (tenants/_templates/ministry_categories.toml)
  until the sending organization's own chart replaces it. A door receipt's
  card line reads its day, its amount (a foreign one as said, with its
  rate), its category ("Uncategorized" when nobody placed it), and its fund.
- **amount** — a door receipt's amount after its photo (board item
  t-receipt-photo, 2026-10-13). "Here's a receipt" with no amount still
  gets the link; the photo lands as `<day>_receipt_unread`. When the
  ministry names a reader (`[expenses] reader`; core/contracts/reader.py,
  `pretend` for practice), the photo's amount is recorded as a proposal
  (`expense.amount_read`) and never prices anything. The person's words set
  it (`set_amount` at the door): the amount they say, read by code, or their
  yes to the reading, recorded as `expense.amount_said` with who said it
  and their words. The month then prices it from that tag exactly as it
  prices a claim said with the photo (a foreign one at its own day's
  rate). Until then the line waits, unpriced: "how much it was" or "their
  word on the amount". No reader on a box = Tim asks how much.
- **drive** — one drive the box's tracker ended (core/mileage; Robb's idea
  2, decided 2026-10-13, option a). The person's phone (Traccar Client)
  reports positions to the box's `/track` with a token only its hash of is
  kept (`trackers.toml`, `engine tracker`); each position adds its step to
  the drive under way and replaces the one before, so the box holds at most
  each person's latest position and drops it when the drive ends (five
  minutes still). The job records `mileage.drive`: the person, start, end,
  miles to a tenth. Never where. What a drive was for comes from the
  person's words in a weekly check-in (next), and its rate from
  core/institution.mileage_rate.
- **date** — a receipt's own day, for one sent later (`set_receipt_day` at
  the door, or what its photo reads; t-catch-up, 2026-10-14):
  `expense.receipt_dated`, the latest word wins. The receipt files in that
  day's month and prices at that day's rate (records.dated_name). A
  catch-up receipt (`catch_up_link`: one link, many files, a day long) lands
  `_receipt_undated` and sits in no month until its day is known. The
  person's own AI reads them at the door (`receipts_to_read`,
  `record_reading`: a proposal in `expense.amount_read`, with payee and
  category), the person agrees to a month's readings in one yes
  (`agree_to_readings`, recorded as their words), and
  `send_month_for_review` runs this job's month for one card.
- **drive_purpose** — what one drive was for, as the person said it
  (`set_drive_purpose` at the door; t-mileage part 2): a purpose, or that it
  was personal (`mileage.purpose`, the latest word wins). `month_report`
  lists each person's drives: miles, what it was for, and a ministry drive's
  dollars at the rate on its own day (core/institution.mileage_rate: the
  institution's, else the IRS table). A drive nobody spoke for waits on their
  word. A ministry drive rides the month's card (the owner, 2026-10-14): one
  line in the reviewer's words ("Oct 12: 14.0 miles, visiting the church
  in Pokhara, $10.15 at 72.5 cents a mile"), filed as a report line once
  approved, a record like a receipt (`records.drive_sha`). Personal or
  unexplained drives never become money. A ministry whose claims go to its
  institution does not send drives yet (AGWM drive claims wait on Robb).
- **statement**, **school**, **line_purpose** — bank statements as the
  backbone of a catch-up (t-statements, the owner's decisions of
  2026-10-14; `statements.py`). The door reads a bank's CSV export (a Debit
  and a Credit column, or one signed Amount) and a school list (student,
  amount, date, school, currency) sent through the catch-up link, holds it
  in memory, and writes nothing until the person says whose account it is.
  The ministry's account: every line is kept (`bank.line`). The person's
  own account: Tim proposes what looks like the ministry's, the person picks,
  and only the picked lines are ever kept; the rest is never written. A
  line's fingerprint leaves the bank's words out (banks reword memos), so a
  statement sent twice is the same lines. Never counted twice: a remittance
  send (Remitly and the like) is money moving, its receipts and the school
  list are the spending; the ministry paying the person back is matched to
  their own statement by amount and day (`bank.transfer_matched`). A
  receipt stands behind the line it paid (day within 3, same cents; in
  rupees, within the bank's 6% of the day's rate), the card's dollars win,
  and it rides that line instead of being a line of its own. A line with no
  receipt waits on what it was for (`bank.line_purpose`). On the month's
  card: what the person paid is owed back and filed as report lines; what
  the ministry paid itself is listed for the yes and never owed; what only
  moved is named in the card's `moved`. School payments count as the
  person's cash spending, at their day's rate.
- **send** — a missionary's receipts to their sending organization, for a
  ministry whose institution keeps its own books (`[institution] submit_to`;
  Robb's idea 1, 2026-10-13). Every receipt not yet sent waits on ONE card
  per person (`expenses.institution_send`, decided by the person's own
  `submit` grant with their Face ID; a new receipt raises a card covering
  all of them and retires the older one). On the yes, the next run sends
  each claim (`core/institution.submit`): what was paid as the person said
  it, its dollar value at its own day's rate when known, their own words,
  and the photo's path; no category (the institution's system places it).
  The institution's reference is an `institution.claim.sent` event, and a
  claim is never sent twice (the sink answers a receipt it already holds
  with its first reference). When the institution doesn't take it, the
  claim is held (`institution.claim.held`, with the one plain sentence the
  person hears) and every later run tries again with no second yes. The
  month job builds no report for such a ministry, so no receipt is both
  claimed from the institution and paid by the books. The person reads
  where each receipt stands in `month_report` (sent, held, going, waiting
  for their yes, not sent yet). A ministry without `submit_to`: no-op.
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
