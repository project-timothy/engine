# A W2 BillPayment is drawn on `[close].bank_account`
Date: 2026-09-12
Type: Two-way door

A QBO BillPayment by check needs `CheckPayment.BankAccountRef`. The W2
design and row 7.2 named no source for it. `[close].bank_account` already
holds "the checking account's name in the accounting system" (the closer's
bank tie-out reads it), and a check is drawn on that account. Decided: W2
resolves that name through the chart of accounts at write time; a missing
or unmapped name parks one `ap.qbo_map_account` card and writes nothing,
since every payment record in the batch is drawn on it. No new config key:
a second name for the same account is a second thing to keep in sync. A
tenant paying from more than one account is a later refinement (a per-row
instrument account), not a reason to invent one now.
