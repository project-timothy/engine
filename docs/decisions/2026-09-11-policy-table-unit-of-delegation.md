# The policy table is the unit of delegation for every lane
Date: 2026-09-11
Type: Refines 2026-09-09

PRD v2 section 4.3. The bank sweep set the shape on 2026-09-09:
`[qbo_sweep].unattended = ["match", "pair"]` names exactly what the machine
may do without a card, and the lane refuses anything outside it. v2
generalizes it. Every lane that can act carries a `[<lane>].unattended`
list in `tenant.toml`, defaulting to empty, and widening delegation is a
data-only PR the owner can read on one screen. The auditor's `approvals`
lens reports every unattended act by lane and count, so delegation is
visible every morning.

This is the "tenant policy table may name specific actions as unattended"
clause of row 4 in the decision table (section 4.1): anything that moves
money, sends anything external, or mutates a system of record is an
approval card unless the tenant's policy table names that act. The model
side gets the same treatment through `[llm.jobs]` (section 5.1), where a
job's tier is data in the same file.

Refines 2026-09-09 (the sweep's scoped unattended exception): the
exception becomes the general rule for delegation, and the sweep's policy
row is its first instance rather than a special case.
