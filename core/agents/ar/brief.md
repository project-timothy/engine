# AR agent brief

## Mission

Catch money coming IN as early as the customer announces it, and close the
loop when the bank confirms it. Built 2026-09-17 after a remittance advice
for $58,250.00 arrived on a Sunday night, named the invoice and the credit
memo that made up the number, and reached nobody: the engine had no AR lane
at all, and the owner noticed three days later by the absence of a line in
the morning brief.

## What it does

- **remittance**: read every mailbox message whose subject carries the
  tenant's remittance marker, parse it BODY-ONLY into payment number,
  payment date, payment amount and the invoice grid, and record it once per
  payment number (`ar.remittance.received`). Then read the bank's own
  statement files and, when a deposit's amount equals a recorded
  remittance's, record that the money landed (`ar.remittance.cleared`).

## Why body-only

A remittance advice carries no attachment. The AP mail feed lists messages
that HAVE attachments and saves the files, so it could never see this mail,
whatever filters it grew. The numbers are in two HTML tables in the body,
and the parser reads exactly those two tables. Attachments are not read
here, ever: a payer who starts attaching a PDF is a change this lane must be
told about, not one it should quietly absorb.

## Where it looks

`[ar].mail_folder` is EMPTY by default, which means the whole mailbox, every
folder included. An advice is routinely filed into a project folder, a
bookkeeping folder, or the deleted items within hours of arriving: on this
tenant's mailbox, not one of the last sixteen advices was still in the inbox.
An inbox-only lane is a lane the owner's own filing habit defeats. A tenant
that wants the narrower read names a folder.

## Gates (tenant config, deterministic)

- **Sender denylist** (`[mail].denied_senders`): the mail lane's privacy
  boundary is the FIRST gate here. A denied sender's body is never opened,
  whatever its subject says, and nothing identifying about it is recorded.
- **Subject marker** (`[ar].remittance_subject`): the only message bodies
  this engine ever reads are the ones whose subject already matched. An
  empty marker turns the lane off.
- **Sender list** (`[ar].remittance_senders`): empty means any sender, so a
  new customer's first advice is read the day it arrives. A tenant that
  wants the narrower gate names the addresses.
- **Deposit markers** and **clearing window** (`[ar].deposit_markers`,
  `[ar].clearing_window_days`): the amount to the cent is the test; the
  markers narrow it when the bank line's wording should count too, and the
  window keeps a like-amount deposit from another month out of it.

## Boundaries (never)

- Never mutates the mailbox: no moves, no mark-as-read, no sends, no
  deletes.
- Never writes the AR register, the accounting system, or any money value
  anywhere. This lane records what the customer said and what the bank did.
  The register card (`ar.remittance_record`) is deliberately NOT built: the
  AR design day settles whether the register stays the system of record or
  the accounting system becomes it, and a card that writes the wrong one is
  a migration, not a feature.
- Never infers an amount or a date. A body the parser cannot read is an
  anomaly (`ar.remittance.unparsed`), never a silent skip.
- Never writes outside the note destination, and only through the write
  guard.

## Escalation

- Unreadable advice: anomaly `ar.remittance.unparsed` with the subject and
  the reason. The payer changed its layout, or the message is not an advice.
- Statement folder present but unlistable: anomaly
  `ar.remittance.statement_unreadable`. "I could not look" is never
  "nothing to look at" (2026-09-13).
- Statement file that does not tie to its own printed totals: anomaly
  `ar.remittance.statement_unparsed`, from the same tie the AP statement
  tier enforces.
- Mailbox auth or transport failure: the run FAILS and leaves its trace
  (#172). A lane that swallowed a dead mailbox would look like a quiet week.

## Input / output contract

Input: tenant config `[ar]` (the gates, the window, the note destination),
`[mail]` (the mailbox the listing reads), `[bank_csv]` (the statement folder
and how its files read), `[close].report_dir` (the note's fallback home), or
fixtures via `--param messages_file` and `--param statement_dir`. Output:
standard `RunResult`; events `ar.remittance.received` (the whole advice as
the payer wrote it) and `ar.remittance.cleared` (payment number, amount,
deposit date and description, statement file). On a day that records
something, one note at `<report_dir>/_ar/remittance-YYYY-MM-DD.md` whose
`## Money in` section is what the morning brief reads. Shadow mode reports
would-record and would-clear lines and writes nothing at all.
