# The owner records the check reference at scheduling time
Date: 2026-09-12
Type: Two-way door

W2 (`docs/w2-billpayment-design.md`, phase 7 row 7.2) records a BillPayment
for every Scheduled row that carries a check reference. Until this day no
production path wrote a `check_ref` onto a row before the money cleared:
`engine status --scheduled` flipped the status and nothing else, and only
reconcile wrote the reference, at settle. W2's scope would have been empty
in real life, and row 7.3 (matching a statement line to an engine-written
payment by check reference) would have had nothing to match against.

The decision: `engine status <tenant> <ref> --scheduled` gains `--check
<n>` and `--date YYYY-MM-DD`, recorded through the existing
`store.record_payment_details` (empty arguments leave the row's values
alone, as before). A row that already wears the target status accepts the
details on their own (exit 0, "already Scheduled; recorded check 4012")
instead of the no-op-transition refusal, so the live rows scheduled before
this day can be given their check numbers by hand. The smallest primitive:
no new command, no new column, no new job. Not in the row's touches list,
flagged in the PR.
