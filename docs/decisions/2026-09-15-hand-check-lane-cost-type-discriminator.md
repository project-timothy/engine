# The hand-check lane, and why the cost type is the discriminator
Date: 2026-09-15
Type: Two-way door

Phase 7 row 7.4 (#213), specified in #257. A cleared payment that no ledger
row explains, coded like contractor work, parks an `ap.record_direct_payment`
card proposing the payable. The owner approves with the project (and the
payee, when the accounting system's feed named nobody) and the next reconcile
run creates the Paid row.

## What this replaces

The first draft of #257 proposed a LENS that flags contractors paid outside
the AP lane as anomalies. The owner killed it in one sentence: *"I'm not
going to change how a 68-year-old man pays for things."* Hand checks to
contractors are permanent, normal operation at this tenant, not a deviation.
A lens that WARNs every time one happens is a nightly nag about the business
working correctly; it would be muted inside a month and the escapes would
resume. So the lane is a first-class PATH, and the engine does what it does
everywhere else: reads, proposes, parks a card.

## The decision that carries the weight

**The registry's `cost_type` is the discriminator, not the GL account.**

A freight vendor (transportation payments are excluded from 1099-NEC
reporting) and a subcontractor (reportable) can code to the SAME
project-expense account, and in this tenant's chart they do. An
implementation that reads the account cards the freight vendor, the owner
learns the lane cries wolf, and it gets muted exactly like the lens would
have been. So:

- a payee the registry KNOWS cards on its cost type alone, and a non-1099
  cost type never cards at any amount;
- a payee the registry does NOT know has no cost type to read, so the
  account is the only signal left and 1099-shaped account patterns card.
  This covers the unregistered contractor and the payee-less payment.

The second branch is deliberately the looser one, because the costs are
asymmetric: a false positive is a card the owner rejects in five seconds,
while a false negative is a missing 1099.

## Why the lane exists at all

A 1099 needs four things maintained by hand in four places (the W-9 paper,
the registry row, a payment attributed to a payee in the accounting system,
and a ledger row); missing any one drops the person. Both detectors that
should catch that start downstream of a ledger row: the vendor-1099 lens
starts FROM the registry, and the CPA appendix builds taxpayer units from
`ap_invoices` plus `expense_report`. A contractor paid direct from checking
has no ledger row, so he is invisible to both and silently absent from the
January packet.

## Subsidiary decisions

**Off by default** (`[qbo].direct_payment_cards`). A tenant whose contractors
all invoice through AP has no such lane, and merging this changes no tenant's
daily run until its owner flips the flag. Flipping it is the behaviour change
that wants an owner present, not the merge.

**The floor is a throttle, not a tax rule** (`[qbo].direct_payment_floor_cents`,
default 60000). The 1099-NEC obligation aggregates across a tax year, so no
single-payment floor is ever tax-correct. This exists so switching the lane on
does not dump every small payee-less Travel and Meals charge into the queue on
day one. The $600 default is the reporting threshold, which makes it a
defensible starting line; 0 cards every candidate and takes the volume.

**A decided card answers its clearing forever, approved OR rejected.** A
rejection is an answer ("this is not a payable"), and re-asking it nightly is
the nag this design exists to avoid. Both statuses enter the explained set.

**The no-guess rule is unchanged** (invariant 2). The queue refuses a card
whose payee or project cannot be known rather than inventing either, so a
card is never approved-but-unexecutable, and nothing is ever created without
the owner's approval.

**Recording is not paying.** No money moves in this lane; the engine records
a payment the owner already made. Invariant 7 is untouched.

## What it does not do

The other half of #213, turning the Thursday sweep's parked feed rows into
cards, is the same machine pointed at a second source and is deliberately not
in this change. That half alters what the sweep does, which wants an owner
present; this half does not, because it is off until a tenant asks for it.
