# Triage 2026-09-18: one proposal, no fixes

Quiet night for code. Lens 19 named an automation that has now been counted
on six subjects, so the eval budget went to a proposal PR.

## New findings

- (recurrence) qbo unknown-clearing, class, 6 subjects in 30 days, INFO. The
  candidate is named: park a card for every hand-written check the ledger
  cannot explain. Candidate id a1b2c3d4.

## Root cause

Not a bug. Six cleared payments carry no ledger row because they were
written by hand, and every one of them is answered by the owner one at a
time. The lens counts the class; nothing retires it.

## Actions taken

- Proposal PR opened for candidate a1b2c3d4: design note
  `docs/proposals/2026-09-18-hand-check-cards.md` plus the failing eval
  `evals/proposals/test_hand_check_cards.py`. No implementation, no source
  change. Merging the eval is the decision.

## Recommended actions for the owner

- Read the proposal PR and merge it if the design is right; the build lane
  takes the row from there. Closing it is a decision too, and a final one.

## Recurring patterns

- Six unexplained clearings in 30 days, all answered by hand: candidate
  a1b2c3d4, now a proposal PR.

## Watch list

- The proposal stays open until it is merged or closed; no second proposal
  for candidate a1b2c3d4 will ever be opened.
