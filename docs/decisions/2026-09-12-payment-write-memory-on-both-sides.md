# A W2 payment write is remembered on both sides, and the engine key is the join
Date: 2026-09-12
Type: Two-way door

Row 7.2 asked for record-then-call-then-record around the QBO write (the
orphan fix, honesty audit 03-F9) and idempotency "on the card key and the
`engine:<key>` PrivateNote". The design named the key
(`qbo-payment:<check_ref>:<tenant>`) and left the rest open. Decided:

1. **Two job records per write.** `ap.qbo.payment_write.started` before
   the create call, `ap.qbo.payment_write.done` the instant it returns,
   before the readback and before the row update; an API rejection also
   writes a done record with an empty id, so a rejected check retries
   plainly (once a day, the intake retry-day rule) instead of reporting a
   lost write.
2. **Heal order.** A done record naming an id for the same rows and amount
   is adopted first (a death between the record and the row update); then
   the accounting system's own copy: a money-out record whose PrivateNote
   equals `engine:<key>` with the same vendor and amount is adopted and a
   done record written for it (a death between the call and the record).
   Nothing found means the call never landed: the write proceeds and
   `ap.qbo.payment_write_retried` names the earlier attempt.
3. **The key stays check-based, per the design, with collision detection.**
   Check numbers can repeat across checkbooks and years, so a key match on
   a different vendor or amount is refused (`ap.qbo.payment_key_collision`,
   no adopt, no write, a human looks) rather than adopted.
4. **The stored id is the evidence form.** `qbo_payment_id` holds
   `BillPayment:<Id>` (the W1 independence eval already used that form),
   so `store.engine_authored_qbo_ids` filters the engine's own payment out
   of the reconcile evidence stream by construction (rule 3); the event
   carries both the raw id and the evidence id.
5. **Readback compares three things:** amount, vendor, and the set of
   linked bills. Any disagreement stops the batch with the id kept on
   every covered row.
