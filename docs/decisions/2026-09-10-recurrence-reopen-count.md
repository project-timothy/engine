# The auditor store counts every return; the recurrence lens gets its third shape
Date: 2026-09-10
Type: Refines 2026-09-10

Lens 19 shipped with a known gap: the store resets `first_seen` when a
resolved finding comes back and kept no count of returns, so a chore
cleared by hand every night (the disk floor, 11 of the first 53 nights)
read as young every morning and never became a repeat. The store now
carries `reopen_count`, incremented on the "returned" branch of reconcile
and added to an existing store on open (idempotent column add), and the
lens reports `recurring` for a finding back `reopen_min` times (default 3).
Counting stays code, naming stays the triage, deciding stays the owner.
