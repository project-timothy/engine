# The statement tier matches on the instrument alone, and names the engine's own record as its qbo_id
Date: 2026-09-12
Type: Refines 2026-09-12

Row 7.3 (issue #212) says what the statement tier must do for an
engine-written BillPayment: match on normalized check reference, the group
sum, and a date window, flip every row sharing the `qbo_payment_id` with
`evidence=statement`. Four things the row left open, decided in the build:

1. **Where the file comes from.** A new `[bank_csv].statement_dir` names
   the folder the owner already keeps exports in (for the first tenant, its
   banking folder, where the bank's export with the default
   `Date, Description, Check Number, Amount` columns already sat);
   the daily script hands reconcile the newest `*.csv` there. The location
   sits beside the format because both describe the same bank export, and
   nothing else in the engine reads a statement file (the close reads its
   anchor off the QBO statement, owner decision 2026-07-21). Adding the
   field re-keys `expenses/match` and `ap/verify-payment` once (they fold
   the whole section); both are owner-run and bookkeeping-neutral.

2. **Scope is money-out check lines, and any open check group.** Deposits,
   ACH, and card lines never enter the tier: a statement carries bank text,
   not a payee the ledger knows, so the only instrument it can speak to is
   a check number. Within checks, the tier settles any open group the
   reference, amount, and window fit, not only engine-recorded ones: the
   bank's own line is the strongest clearing evidence there is, the QBO
   tier's rule 0 already settles on reference plus sum, and a check whose
   payment card the owner never approved would otherwise stay Scheduled
   forever. Candidate groups are each engine payment, loose rows by vendor,
   and the whole set under the reference; one fit settles, several park the
   7.1 review card, none with candidates parks review (a reused number),
   none at all is unknown money.

3. **The paid event's `qbo_id` is the row's own BillPayment.** The auditor's
   QBO lens fetches every `ap.reconcile.paid` event's `qbo_id` from the
   accounting system and calls a missing one evidence-vanished (CRITICAL).
   A statement identity there would fire that nightly. So the event carries
   the engine-written BillPayment as `qbo_id` (it exists, its TotalAmt is
   the group sum the rows claim), empty when the row has none (the lens
   skips), and the statement identity beside it as `statement_id` with
   `evidence: statement`. The unknown event is the opposite: its `qbo_id`
   IS the statement identity, because the reconcile lens keys unknown
   clearings by that field and would hide an empty one.

4. **One physical check, flagged once.** The same check clears in the feed
   and on the statement. An unknown already raised by one source is not
   raised by the other (matched on normalized reference + cents), so the
   owner mutes it once in triage. The QBO path is otherwise untouched: every
   pre-existing reconcile eval runs unchanged.

Refines the 7.1 decision that an owner-resolved settle emits both paid
events: the statement settle emits both too (`resolved_by: statement`),
since the row named `ap.invoice.paid` per row as its acceptance.
