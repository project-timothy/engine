# A card that needs an owner fact is refused at the queue, not stuck after it
Date: 2026-09-12
Type: Two-way door

Phase 7 row 7.1 (issue #210) makes an approved `ap.reconcile_review` card
execute: the owner names the row with `queue approve --param row=<id>` and
the next reconcile run flips it Paid. The row's acceptance says an approval
without the param "is refused with a message naming the candidates" but not
where. The W-9 precedent validates at execution time (an approved card with
no vendor raises `ap.w9_vendor_missing` every run and asks the owner to
"re-approve"), and an approved card cannot be re-approved: it is stuck.

The decision: validation runs at the queue, before the decision is
recorded. `core/engine/registry.load_approval_checks(agent)` reads an
optional `APPROVAL_CHECKS: dict[action_type, check]` from the agent's
`jobs.py`; `Ledger.resolve_approval(..., check=)` runs the check on the
merged params (stored plus `--param` overrides) and raises before any write
when it returns a message; the CLI prints the message and exits 2. A
refused card stays pending with its params untouched. Rejection never runs
a check. Agents that declare nothing approve exactly as before.

The AP check refuses: no `row`; a row outside the card's candidates; a row
already settled (settled is terminal); a legacy group card with no
per-payment detail; and a cleared total that disagrees with the chosen
row's amount, because a review card must never settle a partial (amount is
a confirmation field, never the discriminator: 2026-05-21). A short group
stays pending on purpose: its payments keep re-evaluating, so the
remainder's clearing settles the group through the existing bill-linkage
tier. The same check runs again at execution, since the world can move
between the approval and the next 08:00 run; a card that no longer
executes is named by `ap.reconcile.review_unexecutable`, never skipped
silently.

Review cards now carry a structured `payments` list (qbo id, cents, date,
check ref per clearing) so execution never re-parses display fields; a
single-payment card parked before this date rebuilds it from its own
fields, a legacy group card cannot and is refused with instructions.
