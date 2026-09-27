# The bank statement PDF is the primary clearing source
Date: 2026-09-16
Type: Refines 2026-09-12 (the statement tier matches on the instrument alone)

Phase 7 row 7.4 (#213). Row 7.3 shipped the statement tier reading the newest
CSV export in the tenant's statement folder. The 2026-09-16 triage found what
was actually in that folder: nine monthly statement PDFs the engine had never
opened, beside the one CSV — an export made once, in May, covering six weeks.
109 check numbers sat in those PDFs, 32 of which the engine had never seen,
including both 1099 escapes that were run down by hand two days earlier and one
check whose money the ledger had recorded with no number on the row.

## The decision

**The PDF is the primary statement feed; the CSV stays optional, and the tier
reads the whole folder rather than one file.**

The statement lands on the 1st of every month with zero owner effort. A CSV
export happens only when somebody remembers to make one, which in nine months
happened once. Any design that depends on the owner producing a file is a
design that runs on the owner's memory, and the whole point of this engine is
to stop doing that.

Reading the whole folder every morning is safe because a statement line already
has a content identity (posted date + signed cents + reference, `line_identity`).
The same line in a later file is the same line, so re-reading nine months of
statements every day settles nothing twice and asks nothing twice. That
idempotency is what lets the tier be greedy instead of clever.

## Three sections, one of them evidence

The parse reads all three sections the bank prints — the check grid,
Withdrawals / Debits, Deposits / Credits — because they cost nothing extra once
the file is open, and because the other two have owners waiting:

- **checks** are clearing evidence today, unchanged from 7.3;
- **withdrawals** are what the Thursday bank sweep parks by hand;
- **deposits** (customer remittances) are the AR design day's cleared-receipt
  ground truth.

Withdrawals and deposits are parsed, tied, and recorded in one informational
`ap.reconcile.statement_lines` event per file per run. They change no behaviour
now; they are on the record for when those lanes are built.

**Every section must tie to its own printed count and total, or the file is an
anomaly** (`ap.reconcile.statement_unparsed`), never a silent skip. A statement
half-read is worse than a statement unread: the lines a broken parse drops look
exactly like money that never moved, which is the one shape this engine exists
to catch. A PDF that is not a statement at all (the card statement, a drawing
filed in the same folder) is a log line, not an error — that is a filing habit,
not a failure.

A `0000` check number is the bank saying it could not read the MICR line. The
line is real money, so it parses and carries NO reference rather than an
invented one.

## The 2026 floor

`[bank_csv].statement_floor = "2026-01-01"`. The 2025 statements in the folder
belong to closed, sealed months. Opening them could only re-litigate answers
the close already gave, so a statement whose period ends before the floor is
never opened at all, and any line before the floor is dropped after the tie
(the tie runs against what the bank printed, not against what this engine
cares about).

## Reference backfill, and the line it does not cross

A statement check line with no OPEN row under its number, whose amount and date
fit exactly one SETTLED row carrying an EMPTY reference, writes the number onto
that row (`ap.reconcile.ref_backfilled`) and counts as already recorded.
Nothing flips: the row is already Paid. The live case is a row that settled
from an owner-entered payment the accounting feed gave no number to, while the
bank had the number all along.

**Amount and date never settle an OPEN row.** 7.3's rule stands: the check
number is the discriminator for settling, and amount is a confirmation field,
never the discriminator (2026-05-21). A settled row carrying a DIFFERENT
non-empty reference is a different physical payment and never matches (#134's
boundary). Several fitting settled rows park a review card rather than guess.

## The second card source

A statement check line no row explains feeds the hand-check lane (#258) exactly
as accounting-system money-out does. The statement carries no payee and no
coding — the bank knows the instrument, not the accounting — so neither
discriminator from the 2026-09-15 decision can run. What the statement knows is
stronger for this lane's purpose: a check cleared and nothing in the book
explains it. The bank never loses the check number; the feed routinely supplies
neither a payee nor a document number, and that is how a contractor's payments
come to count toward nobody's 1099. The card asks the owner for the payee and
the project, and the same check never cards twice across the two sources.

## The listing moved into the job

`scripts/statement-export.py` retired with this change. It existed because the
08:00 shell could stat the statement folder but not list it (2026-09-13: macOS
gates `readdir` per program identity, and the denial is silent), so it did the
listing under the interpreter the engine reads that tree with. Now the job
reads the folder itself, under that same interpreter, which is the same fix one
layer in. The incident's rule is unchanged and better placed: a folder that
cannot be listed is an `ap.reconcile.statement_unreadable` anomaly under a run
key that never replays, so it is said again every morning until somebody fixes
the permission — louder than a shell `echo`, and visible to the nightly audit.

## Three rules for the ledger's own legacy references

Added the same day, from the first shadow over nine live statements: it called
29 checks unknown, and **thirteen of them were already-Paid rows whose
`check_ref` is free text a human typed** — "Checks 9032 + 9033", "Check 9025
(cleared 4/3)", "5/3 bill pay 9053", "Check 1025 (electronic image)",
"9056+9057". An exact-token matcher cannot equal any of those. A tier that asks
the owner about money his own book already explains is the nag that gets a lane
muted, which is the same failure mode the hand-check lane was designed around.

All three rules read **settled rows only**. None settles anything and none
writes anything, except the backfill that already existed.

1. **A settled group under the exact reference whose sum equals the line is
   already recorded, whatever the dates say.** Legacy rows carry
   import-artifact payment dates (an invoice date, a Finder tag date), and
   check numbers do not repeat on one account, so reference + amount on a
   settled row is the same physical check and the date window has no work to
   do. The #134 boundary stands: a CONFLICTING reference never absorbs.
2. **A settled row whose reference NAMES the check explains the line.** The raw
   reference is split on non-digits and a whole token equal to the check number
   counts, **provided the token is four digits or more**: free text is full of
   short numbers that are not references (the bank name and the date in "5/3
   bill pay 9053"), and a two-digit fragment counting as a reference would let
   a date silence a real check line. The grid parser never yields a shorter
   number either, so nothing real is lost. No amount test and no date test, because the only outcome is "do not
   flag this as unknown money" and a row paid with two checks answers neither
   line by its own amount. **The accepted risk is a reference typed wrong**: a
   row whose free text names a check it did not pay will silence that check's
   finding. That is the cheaper error — the alternative is a nightly question
   about explained money, and the engine has already learned where those end
   up. Multi-check references are never summed across lines; containment is
   enough.
3. **A reference with no digit in it is not a reference.** "Electronic", "ACH",
   "Zelle" say how the money moved, not which instrument it was, so such a row
   joins the backfill pool exactly like a row with an empty reference. The
   statement's number lands on it and the old word survives in the note
   ("was: Electronic"), because it was somebody's record of something.

Order inside `decide_statement`: exact-reference settled (1) -> the open-row
tiers unchanged -> legacy containment (2) -> the backfill pool (3 widens it) ->
unknown.
