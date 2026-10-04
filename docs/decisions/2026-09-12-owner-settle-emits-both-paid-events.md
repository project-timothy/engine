# An owner-resolved settle emits ap.reconcile.paid and ap.invoice.paid
Date: 2026-09-12
Type: Two-way door

Row 7.1's acceptance names the event `ap.invoice.paid`. That name did not
exist anywhere in the engine or the auditor; every consumer of a settle
reads `ap.reconcile.paid`: the reconcile job's own explained-memory, the
auditor's status-coherence lens (a Paid row needs a reconcile event, a
payment date, or an owner flip), its QBO lens (which fetches each event's
`qbo_id` and compares the claimed `amount_cents` with the accounting
system, so the event must be per payment with that payment's amount), and
the advisory facts' last-settle date.

The decision: an owner-resolved review card emits both. `ap.reconcile.paid`
once per cleared payment, the same payload as a clean match plus
`review_card` and `resolved_by: owner`, so every existing consumer sees the
settle with no change; and `ap.invoice.paid` once per row, the owner-level
fact the plan names (row, all qbo ids, last clearing date, joined check
refs, the card), which is also the execution memory (a card named there
never executes twice). Whether `ap.invoice.paid` should replace
`ap.reconcile.paid` engine-wide is a later row, not this one; nothing else
emits it yet.
