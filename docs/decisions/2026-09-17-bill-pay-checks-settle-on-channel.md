# A bank-written bill-pay check settles its row on channel, amount, and date
Date: 2026-09-17
Type: Refines 2026-09-12

Issue #285. Row 7.3 settles a statement check line by its number: the
number is the discriminator, amount is a confirmation field, and the tier
deliberately never settles an open row on amount plus date alone
(2026-05-21, the near-miss that made invariant 4). That rule assumes the
tenant wrote the check and therefore knows its number.

A bill-pay check breaks the assumption. The bank writes it, so at
scheduling there IS no number: the owner records a send date and nothing
else, the row sits committed with an empty reference, and weeks later the
statement carries a number the ledger has never seen. The line matches
nothing, the money reads as unknown, and with the hand-check lane on it
would card a payable the book already holds. The live case: a small
payable put into bill pay on 2026-09-17 to go out on the 30th.

## The decision

**Option B of the issue: a narrow exception, off unless the tenant asks for
it.** `[qbo].bill_pay_channels` names the registry `payment_channel` values
whose checks the bank writes. A statement check line with no row under its
number settles a committed row when ALL of these hold, and only when
exactly one row does:

1. the vendor's registry entry carries one of those channels;
2. the row is committed money (Scheduled), never payable, never settled;
3. the row carries no instrument reference at all. A row naming a
   different number is a different physical payment (#134's boundary);
4. the clearing fits the tier's usual window around the recorded payment
   date (up to `DEPOSIT_LAG`, 21 days, after it; at most `DATE_SLACK`, 5
   days, before it), and a row with no recorded payment date is not a
   candidate.

Two candidates park the 7.1 review card. Two committed rows for one bill-pay
vendor at the same amount in the same week are indistinguishable once the
bank owns the numbering, and a guess there pays the wrong invoice.

## Why these four and not fewer

The channel is what makes the exception honest: it is the tenant declaring,
per vendor, that no number can exist before clearing. Without it the rule
would read "settle any committed row on amount and date", which is the rule
2026-05-21 forbids. Hand-written checks keep the old rule untouched, because
for those the number exists at scheduling and is the better evidence.

The date window is the tier's usual asymmetric one, `_dates_compatible`, so
there is one definition of a late clearing in the file. **What the recorded
date means on a bill-pay row is why:** it is the day the BANK SENDS the
check, not the day the money moves. The payee deposits it one to three weeks
later, which is exactly the deposit lag the rest of the tier already allows,
and the live case is a send date of 2026-09-30 whose clearing will land in
October. A tight symmetric window would have missed the only shape this rule
exists for. The other direction stays tight: a bank cannot clear a check
before it sends it, beyond a few days of entry skew, so a clearing more than
`DATE_SLACK` before the send date is a different payment.

A row with no recorded payment date is not a candidate at all, even though
`_dates_compatible` passes a missing date. That pass is right when a
reference already named the row and wrong here, where the date is one of
only three discriminators. The cost of a miss is the status quo (the line
reads unknown and the owner answers it once); the cost of a false settle is
a row marked Paid by somebody else's money.

The tier runs after the reference-backfill tier, so money the book already
recorded as paid explains the line first and a committed row that also fits
keeps waiting for its own clearing.

## What it does not change

Nothing until a tenant names a channel: the default is an empty list, so
every existing tenant's 08:00 run behaves exactly as it did. The QBO feed
path is untouched, and it already settled this shape on payee plus amount
once the owner matched the feed line, so this closes the statement's blind
spot rather than adding a new capability.

Option A (type the bill-pay confirmation number at scheduling) was rejected:
it relies on the owner remembering, which is the failure mode the whole 1099
capture-and-escape thread is about. Option C (read the bank's bill-pay
activity export) waits for an export that may not exist.
