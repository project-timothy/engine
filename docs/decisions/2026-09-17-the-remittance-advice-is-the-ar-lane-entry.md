# The remittance advice is the AR lane's entry, and it is read body-only
Date: 2026-09-17
Type: Two-way door (issue #282, the first AR row)

A five-figure remittance advice arrived on a Sunday evening. It named the
payment number, the payment date, the invoice, and the credit memo that netted
it down. Nothing in the engine caught it, and the owner noticed three days
later because a line was missing from the morning brief. The mail lane fetches
ATTACHMENTS, and this message has none: the numbers are in two tables in the
body.

## The decision

**A new `ar` agent owns money in, and its first job reads the advice body-only
and records it, nothing else.** One job, `ar remittance`, runs in the daily
script beside the AP jobs. It emits two events and writes one note. It does
not touch the AR register, the accounting system, or any money value.

## Body-only, and the subject is the gate

The parser reads the payer's two tables: the label/value grid (payment number,
payment date, payment amount, currency, supplier number) and the invoice grid
(invoice number, invoice date, description, amount paid, amount remaining). A
credit memo rides the invoice grid as a negative Amount Paid, which is why the
rows tie to the payment amount and not to the invoice total.

Attachments are never read here. A payer that starts attaching a PDF is a
change this lane must be TOLD about, not one it should quietly absorb.

`[ar].remittance_subject` (default "Remittance Advice") is the gate every
message passes before its body is fetched at all. The live path lists message
metadata and reads the body of a matched message only, so the only bodies this
engine ever opens are the ones the subject already admitted. That is the same
privacy posture the mail lane keeps, achieved by not asking rather than by
filtering after the fact.

## The lane reads every folder, not the inbox

The first shadow run against the live mailbox found nothing, and the reason is
the whole lesson: the 2026-09-14 advice had already been filed into a project
folder. A read-only probe over the mailbox found sixteen advices going back a
year, in eight different folders, three of them in Deleted Items, and NOT ONE
of them still in the inbox. An inbox-only lane is a lane the owner's own filing
habit defeats, silently, in the window between the mail arriving and the next
morning's run.

So `[ar].mail_folder` ships EMPTY, meaning the whole mailbox. The mail fetch
lane keeps its inbox scope, unchanged: it saves attachments, and a wider scope
there would re-file mail the owner already filed.

The wider read is safe because nothing about a non-matching message is
recorded, opened, or hashed. The listing carries metadata only; a body is
fetched one message at a time and only after the gates admit it; and the first
gate is `[mail].denied_senders`, the same privacy boundary the mail lane
enforces, so a denied sender's body is never opened whatever its subject says.

## The sender list is empty by default

`[ar].remittance_senders` narrows the gate and ships EMPTY, which means any
sender. The alternative, a required address list, is a design that runs on the
owner remembering to add a customer before that customer's first payment, and
the whole point of the engine is to stop doing that. The subject marker plus a
body that actually parses is a narrow gate on its own: a message that carries
neither a payment number nor a payment amount is refused, loudly.

The cost is bounded and known: a forwarded copy of the same advice parses to
the same payment number and records nothing new.

## Idempotent on the payment number, never on the run key

One `ar.remittance.received` per payment number, ever. The check is against the
event log, not the run key, because a mailbox that gains one unrelated message
moves the key and yesterday's payment must not be recorded twice. The recorded
and cleared sets ride the key for the opposite reason: yesterday's advice has
to be able to meet today's deposit.

## The bank half is free evidence

The 2026-09-16 statement decision parses and ties the Deposits section of every
statement file already, so the bank's own confirmation costs nothing extra:
a deposit whose amount equals a recorded remittance to the cent emits
`ar.remittance.cleared` once.

**Amount equality is the test**, with two guards: the deposit must fall inside
`[ar].clearing_window_days` (30) of the payment date, so a like-amount deposit
from another month cannot claim it, and `[ar].deposit_markers` (empty, so any
description) can require the payer's wording on the bank line for a tenant that
wants it. Nothing here settles a row or moves money, so a false positive costs
one wrong line in a note; a false negative costs the loop staying open.

One deposit clears one payment: a line another advice already claimed in the
same run is skipped, so two payments of the same amount inside the same window
cannot both close on the single line that only one of them explains.

This job re-reads the statement folder rather than reading the AP job's
`ap.reconcile.statement_lines` events, because that event carries per-file
COUNTS, not the deposit lines themselves. Re-parsing the same nine files is
cheap and keeps the AP job's payload and behaviour untouched; widening that
payload is a change to what the 08:00 reconcile does, for no gain here.

## What is deliberately NOT built

The `ar.remittance_record` card that would flip the AR register's Issued
Invoices rows (step 2 of #282) is out. The AR design day settles first whether
the 05_AR workbook stays the system of record rendered from an engine AR table,
the way AP works, or whether the accounting system becomes it. A card that
writes the wrong one is a migration, not a feature. Every downstream act reads
these events when that decision lands.

## The note is the handoff to the morning brief

On a day it records something, the job writes
`<report_dir>/_ar/remittance-YYYY-MM-DD.md` with a `## Money in` section, one
bullet per remittance. That is the shape the brief's note reader already
parses for the triage and sweep notes, and it reaches the brief in both places
the brief can run, including a sandbox where only the reports tree is mounted
and no engine command exists. A CLI read would have been unreachable there.
