# proposal: materiality surveils from a birth date, so a pre-lens row is acknowledged once

Candidate: e42b0ad2

Lens 13 (`auditor/lenses/materiality.py`) was re-founded on the ledger on
2026-09-04. It fires on an AP row whose amount is over the floor ($5,000) and
over its vendor's trailing-365-day median times the multiple (3x). The lens is
deliberately date-relative — its docstring says so: *"the window is relative to
the row's OWN date so the verdict is the same on a historical pass."*

That property is correct and it has a consequence nobody chose. The book the
lens was born into already held years of rows, and for a row dated February
2026 the amount, the invoice date and the peer median are all immutable. The
verdict is therefore the same tonight, tomorrow, and every night until someone
silences it by hand. The lens surveils the entire history of the book forever,
at a cost of one hand-researched mute per historical row.

That is not hypothetical. The `#188` catch-up wave raised five of them (first
seen 2026-09-08), and all five were answered on 2026-09-11 in
`tenants/<tenant>/triage.toml`. The owner's own comment above that block is the
whole proposal in eight words:

```toml
# Materiality outliers (#188 catch-up wave, first seen 2026-09-08): every row
# predates the lens; amounts eyeballed 2026-09-11 against the source paper
# (or, where no PDF survives, the QBO ELECTRONIC IMAGE clearing).
{key = "8413a42f4dcd2b4a", since = "2026-09-11", why = "Vendor A invoice, amount confirmed against the invoice PDF ..."},
{key = "b987f8bd95f005cc", since = "2026-09-11", why = "Vendor A, three small invoices summed and confirmed ..."},
{key = "1249321b67b129b3", since = "2026-09-11", why = "Vendor A invoice (second copy): no source PDF ..."},
{key = "d7f06a944f46d025", since = "2026-09-11", why = "Vendor A invoice: no source PDF ..."},
{key = "da949f0129c65384", since = "2026-09-11", why = "Vendor B invoice, amount confirmed ..."},
```

Five subjects, five source-document hunts, five hand-written paragraphs. Lens
19 has counted the class five times in 60 days, which is this candidate. And
the cost does not end at the mute: a dated mute keeps applying, but
`auditor/triage.py::aged_entries` reports it again once it passes
`triage_ack_max_age_days` (90) as `acknowledgment-aged`, "re-confirm or
delete". So each of those five becomes a recurring re-confirmation request,
forever, about a row that can never change.

**The pattern this row ports already exists two files over.** Lens 14
(`check_gaps`) bounds its own surveillance by date — `if upper - lower <= 1 or
seen[upper] < cutoff: continue`, a 90-day window — so a historical check gap
ages out of the nightly report on its own. Lens 15 (`reconcile`) does the same
with `unknown_window_days` (30). Materiality has a window too,
`materiality_window_days` (365), but it bounds only the PEER comparison and
never the surveillance. This row does not invent a mechanism; it gives lens 13
the bound its siblings already have.

## Design

**The mechanism.** One new tenant knob and one partition, both in the lens.

1. **`[auditor.materiality].surveil_from`**, an ISO date, parsed in
   `auditor/config.py` into `materiality_surveil_from: date | None`. **Absent
   means today's behaviour exactly** — every row surveilled — so no tenant's
   report changes until its owner says so.
2. **A row is BACKLOG when its ledger arrival precedes that date.** The gate is
   `ap_invoices.created_at`, the moment the row entered the book, never
   `invoice_date`.
3. **Backlog rows leave the nightly outlier test** and are summarized in
   exactly ONE finding — INFO, condition `materiality-backlog` — naming how
   many there are and each one's fingerprint, so the owner can confirm them as
   a batch and so nothing is excluded silently.
4. **Rows that arrived on or after the date behave exactly as they do today**,
   WARN and all.

**Why the gate is arrival and not the invoice date.** This is the part that
decides whether the row is safe, and the obvious version is the unsafe one.
Rows #161 and #162 entered this book on 2026-09-17 carrying invoice dates of
2026-03-05 and 2026-03-27 — months before any plausible birth date. A rule
keyed on `invoice_date` would have exempted both the moment they were written.
Both happen to sit under the $5,000 floor, so neither would have fired either
way: this is a trap the design must avoid, not a bug already biting. But the
back-dating path is live and routine — the hand-check lane (`#258`) exists
precisely to turn historical hand-written checks into ledger rows, and
`tenants/<tenant>/triage.toml` already lists the March 1xxx checks waiting to
become exactly that. An `invoice_date` gate would make every one of them
invisible to lens 13 on the day it arrived, which is the one day it matters.
`created_at` cannot be back-dated by the document; it is the book's own memory
of when it learned the fact.

**Why a configured date and not "the date the lens first ran."** The
self-installing version is tempting and wrong. Deriving the birth date from the
auditor's own store would make the lens read its own history to decide what to
report, and the auditor's whole worth is that it re-derives from ledger state —
the 2026-09-17 incident entry says it in as many words, that a lens which
trusts a stored verdict lets one bug suppress a real finding. A date in the
tenant file is inspectable, owner-controlled, diffable, and cannot drift.

**The approval shape.** None. This is a read-only lens with no money meaning;
it changes what a report says, never what the engine does. The owner's act is
setting one date once.

**What it must never do.**

- Never suppress a row that arrived on or after `surveil_from`, whatever date
  the document carries. That is the back-dating trap above.
- Never suppress a NEW outlier. The lens exists to catch the mis-extracted
  `$148,855.00` of its own docstring, and that row arrives today by definition.
- Never change a tenant's report when `surveil_from` is unset. An absent knob
  is the default configuration, not an opt-out (the convention the 2026-09-04
  lenses already follow).
- Never write `triage.toml`. The backlog is reported; muting stays the owner's
  file. This row reduces how often the owner must write there; it never
  writes there for the owner.
- Never drop a backlog row silently. It is named once, with its fingerprint, so
  the record of what was set aside exists in the report itself.
- Never use the finding store's new/returned memory as the gate. "Have I
  mentioned this before" is not the same question as "did this row predate me",
  and answering the second with the first is how a real outlier goes quiet
  after one night.

**What this does not fix, deliberately.** The five already-muted subjects stay
muted; this row does not retract anyone's mute or rewrite history. It stops the
SIXTH from costing a hunt and a paragraph. And it does nothing about the
`acknowledgment-aged` re-confirmation tail on mutes already written — that is
lens 18's own question and a separate row.

## Failing eval

`evals/proposals/test_materiality_surveils_from_a_birth_date.py`, two
assertions:

1. **`test_a_pre_birth_row_is_acknowledged_once_not_nagged_nightly`** — rows
   that entered the book before the birth date partition out of the nightly
   outlier test and come back as a single INFO backlog finding carrying the
   count and the fingerprints; with `surveil_from` unset, every row is
   surveilled exactly as today.
2. **`test_a_backdated_row_that_arrived_after_the_birth_date_is_still_surveilled`**
   — a row whose invoice date is long before the birth date but whose ledger
   arrival is after it still fires. This is the #161/#162 shape and it is what
   pins the gate to arrival rather than to the document.

**Why it fails today.** `auditor.lenses.materiality` has no
`partition_by_arrival` and the tenant config has no `surveil_from`; the lens
surveils every row in the book on every pass. The eval fails on the missing
helper, by design, and stays red until the build lane makes it pass and moves
it into `auditor/evals/`.

## Issue

**Goal.** A row that was already in the book when lens 13 was born stops being
a nightly WARN that only a hand-written mute can silence. It is named once, as
a batch the owner can confirm in one sitting, and the lens spends its nightly
attention on money that arrived after it started watching.

**Acceptance.**
- With `[auditor.materiality].surveil_from` set, a row whose `created_at`
  precedes it raises no `amount-outlier` finding; the same rows appear once in
  a single INFO `materiality-backlog` finding naming the count and each
  fingerprint.
- A row whose `invoice_date` precedes `surveil_from` but whose `created_at`
  follows it still raises `amount-outlier` (the #161/#162 shape).
- With `surveil_from` unset, the lens's output is byte-identical to today's on
  the live tenant.
- A row over the floor that arrives after the date still fires on the first
  night it is seen.
- `evals/proposals/test_materiality_surveils_from_a_birth_date.py` passes and
  moves to `auditor/evals/`.

**Touches.** `auditor/lenses/materiality.py` (the partition and the backlog
finding), `auditor/config.py` (`surveil_from` -> `materiality_surveil_from`),
`tenants/<tenant>/tenant.toml` (`surveil_from = 2026-09-04`, the lens's founding
date), `docs/auditor-design.md` (lens 13's entry).

**Size.** S. One date, one partition, one summary finding. The care is in the
arrival-not-invoice-date gate and in proving the unset default is inert.

**Depends.** Nothing blocking. The lens, its thresholds and the finding
fingerprint all exist and are unchanged by this row.

**Door.** Two-way. Unset the knob and the lens surveils everything again,
exactly as tonight. The worst failure mode is a report that names a backlog it
should have surveilled, which the single INFO finding keeps visible rather than
silent.

**Retires.** Candidate `e42b0ad2` (`materiality`/`amount-outlier`, 5 subjects
in 60 days, every one a row that predates the lens).
