# A sweep card is keyed by the feed line, and the sweep-cards job runs in the 08:00 daily
Date: 2026-09-17
Type: Two-way door

Phase 7 row 7.4 (issue #213), the third card source. The weekly bank-feed
sweep parks the feed rows its policy may not click into a dated note under a
"Needs <owner>" heading. Those rows now become `qbo.sweep_parked` approval
cards. Two things the row did not specify had to be decided.

## The card's identity is the bank's facts, not the note's sentence

The note is written by a model session, so the same feed row is worded
differently from one week to the next ("still waiting from last week", a
reordered proposal, a fuller explanation). A card keyed on the sentence would
ask the same question every Thursday, which is the nag every lane in this
engine is built to avoid.

So the key is `sha256(date | amount_cents | direction | BANK TEXT)[:16]`.
None of those four is the writer's choice: the date and the amount are the
bank's, the direction is the bank's, and the bank text is the run of capitals
the bank itself prints on the line. Two genuinely different rows collide only
when they share all four, which would be indistinguishable in the note as
well.

The cost is a dependency on the note's line shape, so the shape is pinned in
the tenant's `skills/qbo-sweep/SKILL.md` as part of the note contract: the
date, the feed card, the bank text and the amount, pipe-delimited, written off
the feed row and never paraphrased. The parser is tolerant of prose around
them (it takes the first date and the first amount in the bullet and the
longest run of capitals), so a session that writes a sentence instead of a row
still produces a usable card; what it loses is the guarantee that next week's
wording keys the same.

## The section is found structurally, never by the owner's name

The heading in a real note names the owner. Nothing under `core/` may spell a
tenant's owner (invariant 5), so the rule is "a heading whose text begins with
`needs`". That works for "Needs <owner>", "Needs owner", and any other
tenant's spelling, and it keeps the bleed-through lint green.

## The job runs in the 08:00 daily, not in the sweep wrapper

The sweep wrapper could call it the moment the note is installed, which is
sooner. It does not, for three reasons:

1. The wrapper's world is deliberately walled off from the ledger. It stages
   read-only JSON and drives a browser; the session never opens the ledger.
   Adding an engine job there means the ledger's write lock and the canonical
   state paths inside a wrapper that pins neither, and a run from the wrong
   place forks a ledger.
2. An interactive sweep writes a note without the wrapper. Wiring the job to
   the wrapper would silently skip exactly those notes, which on this host is
   most of them lately.
3. The daily run already owns the lock, the paths, and the freshness guard,
   and it runs every morning, so a note from any source is picked up the next
   morning with no new schedule entry and no new failure mode.

The cost is latency: cards appear the morning after the note rather than the
minute after. Nobody is waiting on them; the answer they carry is used a week
later by the next sweep.

## Posting an approved card stays outside the unattended grant

The row's goal says the next sweep "posts approved cards". It lists them. The
sweep's unattended grant is `match` and `pair`, and an approved coding is an
Add, which creates coding in the books. The owner's answer authorizes the
coding, not a new class of unattended browser write, and widening
`[qbo_sweep].unattended` is a policy change with its own evidence. So
`post-these.md` is a section the headless note carries and an interactive
session can act on when the owner says so.
