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
  until the sending organization's own chart replaces it. A ministry adds
  what it spends on beyond that list to its own (a scholarship ministry's
  Scholarships, where its school list files). A door receipt's
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
  in Pokhara, $10.64 at 76 cents a mile"), filed as a report line once
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
  statement sent twice is the same lines, and a run writes only lines and
  balances not already kept. The bank's balance (board t-books-page,
  2026-11-11; `balances.py`): when the export has a Balance column, the
  ministry's account keeps, per statement, each day's closing balance and
  every line it held (`bank.balances`; the day's last line in the order the
  balances follow; a blank one is never guessed). The books are checked
  only with lines a balanced statement held, so another account's lines, or
  a statement sent with no balance, never read as a gap. The earliest
  balance is where the books start; each later one checks them, and a gap
  names its days and dollars once. `books` answers `in_the_bank`. The person's own account never keeps a balance. Never counted twice: a remittance
  send (Remitly and the like) is money moving, its receipts and the school
  list are the spending; the ministry paying the person back is matched to
  their own statement by amount and day (`bank.transfer_matched`). Cash
  from an ATM (board t-atm-cash, 2026-10-15) is money moving too: the
  withdrawal waits on the rupees it gave (the slip, or the person's word
  through `explain_bank_line`, kept as the purpose's `cash`), which set what
  each rupee cost; rupee receipts no card line carries and rupee school
  payments spend that cash down oldest first at that cost (`statements.cash`;
  cash is drawn on the day the bank's words carry, `Line.drawn_on`, else up
  to 3 days before it posted, since a weekend withdrawal abroad posts late;
  priced through `fx.priced(..., drawn)`), paid by the ministry when the cash
  came from its account, so nothing is owed back; month_report says the
  cash on hand. The bank's ATM or foreign fee is spending, never waiting
  (Bank Fees, else Other). The cash is counted (board t-cash-count, 2026-10-15):
  `count_cash` takes the person's word for what's in hand (kept as
  `bank.cash_counted`) and says the gap against `statements.cash`;
  `explain_cash` says where the gap went, the ministry's spending or their own
  use (`bank.cash_used`, the expenses `cash` job; an amount from their words,
  a bare number asks, none takes the whole gap). Ministry cash used with no
  receipt is the ministry paid; their own cash on the ministry is owed back;
  the ministry's cash on themselves is "owed to the ministry" (role REPAY,
  never spending, never on the card's total: the card's `repay`). A line on
  the ministry's account bought for themselves is the same: `explain_bank_line`
  with `whose` own keeps it on the purpose (`whose`), and the line reads REPAY
  (Caleb's first night, 2026-10-25). A send home on the ministry's account
  that was their own reads REPAY too (medicine for themselves, 2026-10-29).
  **line_split** (`split_line`) takes an amount out of one line in the
  person's words, in the line's money, never a percent (`bank.line_split`
  with `take`, the owner 2026-11-10): "1,300 of that was mine" (whose own: owed
  to the ministry when its money paid; nothing when their own account did)
  or "5,000 of that was scholarships" (a category). The rest stays where it
  was. One take per kind; the same kind said again replaces it. The line
  reads as its parts, adding to the line to the cent (largest remainder);
  only spending splits. Splits by whole percent (2026-10-29) are no longer
  written but stay readable: an amount comes off the whole first and the
  percents divide what is left. Tim never asks for a split. On a receipt
  (`events#`), `split_line` with whose own is the receipt split below.
  Both explain tools name the category
  their words point to; with none, they list the ministry's categories and
  take the one the person agrees to back as `category` (off the list: nothing
  written). A receipt's split keeps the ministry's part as the receipt's amount and the
  rest as `expense.receipt_split`: a line still carries the whole receipt and
  splits in the same proportion. The ministry's cash pays first, then the
  person's own; a purchase from both owes back only their part
  (`fx.Drawn.own_cents`, the owner 2026-10-16). Cash ages (t-advance-clock):
  month_report's `cash_oldest` from 45 days, the card's `cash` past 120, and
  the routing notices ask for a count on the 1st and once at 45 days. A receipt stands behind its own person's line it paid, never a bank fee, every receipt that can find a line seated (day within 3, same cents; in
  rupees, within the bank's 6% of the day's rate), the card's dollars win,
  and it rides that line instead of being a line of its own. Two rupee
  receipts that could each be one line, one left over, are never guessed
  (t-ask-which-receipt): neither is cash or a line, the line waits, month_report
  lists `could_be`, and `pick_card_receipt` records the person's answer as
  `expense.receipt_rode` (the expenses `cash` job, `what = rode`). A line with no
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

## budget (t-year-budget, 2026-10-15)

`engine run <t> expenses budget --param year=2026 --param kind=Scholarships
--param amount=12000.00 --param by=<person> --param said=<their words>`: one
`expenses.budget_set` event, the kind spelled as the ministry's own category.
The newest one said for a kind and year is the budget; the year report
(core/tools) reads it back beside the month report's own dollars: spent,
left, and how much of the year is gone (core/agents/expenses/budget.py).
Holds and moves no money. The door's `set_budget` offers it only to someone
who looks over every month.

## close_year (the owner, 2026-11-10: the books close once a year, by calendar year)

`engine run <t> expenses close_year --param year=2026 --param digest=<sha256>
--param by=<person> --param said=<their words> --param what=<the card's words>`
plus the year's numbers (`came_in`, `spent`, `spent_by_kind`, `owed_back`,
`owed_to_ministry`, `waiting`): one `expenses.close_year` card
(core/agents/expenses/year_close.py), decided as a month's review is (approve
on expense.report; it commits no money, so the door takes Face ID or the
emailed yes). The door's `close_year` computes the numbers with the books
tool's own code and offers it only to someone who looks over every month, once
the year is over; months still waiting on a yes show on the card and never
refuse it. The same numbers asked again are the same card; a card turned down
is asked afresh. A yes closes the year: the door refuses every write naming
something dated in it in one sentence (a late receipt's day is offered the open
year), the statement and school jobs leave its lines out, and a month of it
raises no new review card. Reads are untouched. Events and cards only; no
schema change. A ministry that never closed a year keeps every key it had.

## reopen_year (the owner, 2026-11-10: the right people for each size of ministry)

`engine run <t> expenses reopen_year --param year=2026 --param by=<person>
--param said=<their words>`: one `expenses.reopen_year` card for a closed year,
carrying the warning that reopening may change the numbers last year's 1099s
and 990 came from. Its rule is approve on books with `submitter=by`, so the
ministry's own safeguards say who answers: a solo ministry (self-approval
allowed) lets the missionary reopen alone; a church or an organization
(self-approval never) needs someone else who approves for the books (a
church's pastor for its treasurer; another person in finance). It commits no
money, so the door takes Face ID or the emailed yes. The latest yes stands: a
reopen's yes opens the year, and a close after it closes it again (that close
card's key carries `:after-reopen-N`). Each close gets its own reopen card; one
turned down is asked afresh. A ministry that never closed a year keeps every
key it had.

## letter_detail (c32, donors and their letters; the owner, 2026-11-12)

Keeps what the donor letters need that the books don't hold, as said at the
door (`letter_details`): the ministry's EIN, its mailing address, who signs
and their title, a sentence on what gifts did, and a donor's mailing address.
One `donors.letter_detail` event per word said; the newest on each field
stands. No money, no ledger row: the letters themselves are read from the
books (`donors.py`) and written as pages and PDFs by `engine donor-letters`,
which sends nothing. A gift is a ministry deposit explained with `gift_from`
(`fund_grant` for a donor-advised fund's grant) or booked under a kind named
"Gifts from <name>".

## payer, payer_form (c33, payers and the tax packet; the owner, 2026-11-12)

Keeps who pays a missionary, as said at the door (`keep_payers`): one
`payers.payer` event per payer said (the person's unit, the payer's name, its
kind: support platform, sending church, agency, or personal gift; the words
the bank prints on its deposits; W-2 for an agency that employs them), the
newest word on a payer standing. In January, one `payers.form_figure` event
per payer and year (what its tax form reports, in cents, read by the door from
the person's words). No money moves, no migration, no ledger row. The packet
(`tax_packet`, `payers.py`) reads the year's bank rows: a deposit that was the
person's own (`their own money in`, c31) matches to the one payer whose words
it carries (two as likely, or none, is the person's to say); a personal gift
is listed and kept off; a deposit still counted as the ministry's that carries
a payer's words is asked about, never moved. A deposit on their own account
that is a `their pay` line of the ministry arriving (the same person, the same
dollars, within a few days, matched as a payback is) reads as `their pay
received` (#651, the owner, 2026-11-16): the same money as the pay line, kept
beside it on the packet, never paid back and never added. Code decides it;
nobody is asked. A line named someone's pay stays their pay even when keeping
their own statement had matched it as a payback first, and keeping their own
statement never matches a pay line as one (#653). A missionary's packet is theirs
the missionary's, whoever looks over every month (who may also keep their tax
facts and trips, naming them), and anyone they share it with. The owner,
2026-11-15, after decision 2: the president does the missionary's books, so
decision 2's "theirs alone" waits for a missionary with a tax picture of
their own.

c38 PR 2 (gaps B and D): a W-2's form figure is its box 1 wages, with box 2,
federal income tax withheld, on the same `payers.form_figure` event
(`withheld`); wages never reach Schedule C, and take-home pay is never
compared against them. A payer kept as a personal gift whose name reads as an
organization, or whose deposits come about monthly (three in a row, 25 to 38
days apart), is asked once, "Do they send this because of your ministry
work?"; the answer is kept on the payer event (`work`, yes or no) and carried
when the payer is said again. A yes is income with no tax form, on Schedule C
(`with_no_form`). A deposit of their own money explained with `gift_from` is a
gift for them on that one line, whatever payer its words match, and never a
donor's gift to the ministry (donor letters read `came in` lines only). A
payer whose form isn't kept yet says to keep what it says once it comes, even
when it differs (persona finding Y02).

## tax_fact, tax_travel (c38, what only the person tells Tim; the owner, 2026-11-15)

Keeps what the missionary says about their own taxes, at the door
(`keep_tax_facts`, `keep_travel`, `core/tools/tax_facts.py`). One `tax.fact`
event per call, for a tax year: filing status (single, or married filing
jointly; any other is left to the preparer and nothing is kept), a spouse's
wages and federal tax withheld (only as a pair, only on a joint return), last
year's total tax, an estimate paid with its day, and who else may read the
packet (`shared_with`, units on the ministry's list, or "no one"; no year).
Newest wins field by field; an estimate said again for its day replaces it.
One `tax.travel` event per trip: the day they left the United States, the day
back (empty while away), the country; newest wins per day left, and `forget`
drops it; an overlapping trip is asked about and nothing kept. The person says
these, or whoever looks over every month for them; the packet shows them back
as `what_they_said`.
No money moves, no migration, no ledger row.

The packet's tax half (`packet.py`, pure): Schedule C income is every 1099
payer's form figure, or what Tim saw when no figure is kept yet; costs are
every line on their own account they said was paid from their own pay
(`whose` pay, role `paid from their pay`, c33): never owed back, never the
ministry's spending, so the books and expense reports leave it out.
Self-employment tax
is 12.4% (to the wage base) plus 2.9% of 92.35% of the net, each part rounded
as the form does, none below $400 of earnings; a W-2 agency's pay carries
none. The days abroad are a draft from the days the bank shows spending in
another currency, with each gap of more than two weeks named for the person.
The estimate dates move past a weekend, Martin Luther King Jr. Day, and
Emancipation Day. Each year's figures sit in `packet.YEARS` with their source;
a year without them says so and works out no tax. `packet.page` lays the
answer out as one page of blocks (formal register, every acronym spelled out),
and `engine tax-packet` writes it as a page and a PDF through `core/tools/paper.py`,
the donor letters' writer; it changes no books and sends nothing.


c38 PR 3 (gap E): the packet's days abroad are counted from the trips said
(`core/tax/presence.py`, pure): full days only, so the day they left and the
day they came back don't count; the best 12 months in a row (365 days, 366
across February 29) among those reaching into the tax year, then the one most
in the year; a trip still open runs to the period's end, and the days after
today are named (`days_still_ahead`). The bank's foreign spending stays beside
it as the cross-check: each day it shows spending abroad that falls on no
trip said is named (`bank_abroad_on_days_home`). With no trips said, the
bank's draft stands as before.
## processor (c32, gifts through a payment service; the owner, 2026-11-12)

Keeps a payment service's export (PayPal, a church giving app; any CSV with a
day, a gross, and a fee or a net, read by `processors.py` by header name),
sent through the catch-up link and confirmed at the door as the ministry's own
account (`bank_statement` with `account=ministry`, plus `service` when the
export doesn't say which). One `processor.payment` event per completed gift,
refund, or payout, keyed by the row's transaction id; pending, failed, holds,
conversions, and money spent from the balance are left out and named back. No
migration, no ledger row. The month reads each gift as money in at its gross
with its donor named (`gift_from`; a giving fund's grant sets `fund_grant`)
and its fee as spending from the service; the service is its own account in
the journal (`Assets:Processor:<service>:<person>`). A payout is matched on
every read to the bank deposit it became (same dollars, that day or within a
week after, the same person's ministry account): the deposit carries the move
(`moved between its accounts`), never income, and the 990's gross receipts
count the gift once, at the service. A fee is no one's yes: it never goes on
the reviewer's card.
