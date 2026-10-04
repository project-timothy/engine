# proposal: nested inbox skip cards supersede older ones, so one answer answers them all

Candidate: 99e8ae57

The receipt-inbox lane (`core/agents/expenses/jobs.py`, `_inbox_run`, issue
#112) parks one `expenses.inbox_skip` card per run covering every image at the
inbox root the classifier labelled "not a receipt". The card's key is a content
fingerprint of that set — `inboxskip:<sha256 of the sorted sha256s>[:16]` — and
that is the right choice: a key shaped like an identity (the folder, the month)
is the exact defect the close-lock lane already paid for, and the lesson "a
reject means ask again" names it.

The consequence nobody chose is structural, and it follows from two facts that
are each correct on their own:

1. the files only leave the inbox root when the owner APPROVES the card (they
   move to `_not-receipts/`, never deleted);
2. so tonight's unanswered set is still there tomorrow, and tomorrow's card
   covers it **plus** whatever arrived since.

Each night's card is therefore a strict SUPERSET of the last, with a different
key, so the queue's dedup correctly declines to collapse them and both cards
stay pending. N unanswered nights leave N nested pending cards asking one
question at N levels of detail.

The lane raises the fresh key. Nothing ever retires the card the fresh key
replaced. The lessons file already states the whole contract — *"a re-ask rides
a fresh key naming the card it supersedes"* — and this lane implements the first
half of that sentence and not the second.

**What that cost, live.** One September run of this shape produced four pending
skip cards covering 2, then 3, then 5, then 9 files, each set containing the one
before it. The owner approved the 9-file card, which answered every question the
other three carried, and the three subsets stayed pending until he closed them
by hand five days later: three clicks on questions already answered in full, and
four `approvals / stale-pending` WARN findings on the nightly report in the
meantime — which is this candidate, counted by lens 19 as a class of four
subjects. Particulars are in the 2026-09-30 triage note, which stays private.

**Why this is worth a row and not a shrug.** The stale-pending class is not
noise about a slow owner; it is the report telling him a decision is outstanding
when it is not. Every one of those WARNs is false by the time it prints, and a
surface that cries wolf about the approval queue is the surface the hand-check
lane and the close lane both depend on being believed.

## Design

**The mechanism.** One optional field on the approval contract, one containment
test in the lane, one status the runner writes.

1. **`ApprovalSpec` gains `supersedes_keys: list[str]`** (default empty, so every
   existing lane is untouched). `core/engine/contracts.py`'s own docstring is why
   the field goes here rather than the lane doing the work: *"the runner owns all
   persistence ... so agents never touch the ledger's write internals."* A lane
   may DECLARE that its new card replaces older ones; only the runner may close
   them.
2. **The lane computes the declaration from set containment, in code.** Before
   parking, `_inbox_run` reads the lane's own PENDING `expenses.inbox_skip`
   cards, parses each one's `sha256s` param into a set, and names every card
   whose set is a **subset** of the set the new card covers. Deterministic, no
   model in the path (invariant 2): the sha256 list is already in the card's
   params and is already the thing the key is built from.
3. **The runner closes each named card with status `superseded`**, recording an
   event that names the card that took over and the card it replaced. Only the
   two statuses `approved` and `rejected` exist in the queue today, and every
   reader keys off an explicit value, so a third value is inert to all of them —
   see the fences below for the one exception that must be checked.
4. **The invariant this buys, and the acceptance test:** the inbox lane never
   holds more than one pending skip card, whatever the owner does or does not do.

**Why at park time, and not "when a superset is approved."** The candidate
sentence says a stale card closes itself when a superset is approved, and that
version works, but park time is strictly better and no harder. At park time the
subset card never becomes stale at all, so the WARN never prints; at approval
time the report still nags for as many nights as the owner waits, and the fix
only tidies up afterwards. Park time is also synchronous with the run that
created the superset, so the two writes live in one place instead of depending on
a later run to notice. The cost of park time is that a card the owner is looking
at can close under him — which is the situation he is already in, because the
lane replaces his card with a better one every night regardless; the difference
is that afterwards there is exactly one card to look at instead of five.

**Why containment and not "the newest card wins."** Newest-wins is one line
shorter and silently wrong. A file the owner moves out of the inbox by hand
leaves the next night's set SMALLER, and a card covering files the new card does
not cover must keep asking about them. Containment is the only test that
guarantees every question in a closed card is still on the table in the open one.

**The approval shape.** None, and this is the part to read twice. Supersession is
queue hygiene, not a decision: a superseded card is `superseded`, never
`approved`, so the lane's execute path — which selects approved cards only —
moves no file on its behalf. The owner's set of possible answers is unchanged;
only the number of times he is asked the same question changes.

**What it must never do.**

- **Never supersede a card whose set is not a subset.** The hand-moved-file shape
  above. A card with one file nobody else is asking about stays pending.
- **Never supersede across action types.** A receipt-filing card
  (`expenses.inbox_file_receipt`) asks a different question about a different
  file and is never a subset of a skip card, whatever the sets look like.
- **Never supersede a card the owner already answered.** Only `pending` rows are
  candidates; an `approved` or `rejected` card is a fact and stays one.
- **Never let `superseded` read as `approved`.** The lane's own execute path
  selects `status = 'approved'`, so it is already safe; the fence that must be
  checked is any reader that treats "not pending" as "decided" — the bank sweep's
  decided-set query is written that way on purpose for its own lane, and a
  reader like that must opt in to counting a supersession, never inherit it.
- **Never supersede an EQUAL set.** Equal sets mean equal keys, which the queue's
  dedup already handles as the designed memory; a containment test that accepts
  equality would close the card the dedup just declined to replace.
- **Never close a card silently.** The supersession is an event naming both card
  ids, and the run summary counts it, so "one card where there were four" is
  visible in the record rather than inferred from an absence.

**What this does not fix, deliberately.** The other rejected card in the live
shape is a receipt-filing card, and the dropped-ask WARN behind it is a
different defect with a different cause: that lane's re-ask keys on the FILE's
identity, so one resolved card blocks every later ask about that file forever —
the "dedup key shaped like an identity" disease, in the second lane to catch it.
That is a separate row against the same lesson, and this one does not touch it.
Nor does this row change what the classifier decides, how confident it has to
be, or where a skipped image lands.

## Failing eval

`evals/proposals/test_nested_inbox_skip_cards_supersede.py`, two assertions:

1. **`test_nested_skip_cards_are_superseded_not_answered_one_by_one`** — three
   pending cards whose file sets nest (2 ⊂ 3 ⊂ 5) and a fourth run covering all
   five plus four more: every one of the three is named for supersession, the
   plan names the superseding key for each, and the resulting status is
   `superseded` and not `approved`, so nothing executes on a closed card's
   behalf. This is the live September shape with placeholder digests.
2. **`test_a_card_holding_a_file_the_new_set_lacks_is_never_superseded`** — the
   converse fence, and the one that stops this row from being "newest wins": a
   pending card covering a file the new set does not cover stays pending, an
   equal set is never superseded, and a card of a different action type is never
   a candidate at all.

**Why it fails today.** `core.agents.expenses.jobs` has no
`skip_card_supersessions`, and `ApprovalSpec` has no `supersedes_keys`: the lane
parks its fresh card and leaves every older one pending. The eval fails on the
missing helper, by design, and stays red until the build lane makes it pass and
moves it into `core/agents/expenses/evals/`.

## Issue

**Goal.** The receipt inbox asks its skip question once. A run that parks a
broader skip card retires the narrower pending cards it already covers, so the
owner answers one card instead of one per night waited, and the report stops
reporting decisions that are not actually outstanding.

**Acceptance.**
- After a run that parks a skip card, at most one `expenses.inbox_skip` card is
  pending for the tenant.
- A pending skip card whose `sha256s` set is a strict subset of a newly parked
  card's set is closed with status `superseded`, and an event names both card
  ids; the run summary counts the supersessions.
- A pending skip card holding any sha the new card does not cover stays
  `pending`.
- A superseded card is never executed: the lane's approved-card path moves no
  file for it, in live or shadow.
- `expenses.inbox_file_receipt` cards are never superseded by a skip card.
- Existing lanes that declare no `supersedes_keys` behave byte-identically.
- `evals/proposals/test_nested_inbox_skip_cards_supersede.py` passes and moves to
  `core/agents/expenses/evals/`.

**Touches.** `core/engine/contracts.py` (`ApprovalSpec.supersedes_keys`),
`core/engine/runner.py` (close the named pending cards as `superseded`, one
event each), `core/agents/expenses/jobs.py` (`skip_card_supersessions` and its
call in `_inbox_run`, plus the summary counter), `docs/lessons.md` (extend "a
reject means ask again, and a summary reports only what happened" with the
second half of its own sentence: a fresh key that names the card it supersedes
must also retire it).

**Size.** S/M. The containment test is a dozen lines and the runner change is a
status write and an event. The care is entirely in the fences: subset not
newest, strict not equal, one action type, and `superseded` never reading as
`approved`.

**Depends.** Nothing blocking. The card, its key, and its `sha256s` param all
exist today and are unchanged by this row. It composes with, and does not
require, the separate row for the file-identity re-ask key.

**Door.** Two-way. A lane that declares no supersessions is today's behaviour
exactly; reverting the declaration in the inbox lane restores the nested cards
without touching the queue's schema. The worst failure mode is a card closed
that should have stayed open, which the containment fence makes impossible for
any file not already covered, and which the supersession event makes visible
rather than silent.

**Retires.** Candidate `99e8ae57` (`approvals`/`stale-pending`, 4 subjects in 60
days, three of them nested skip cards the owner answered twice). The fourth
subject is the receipt-filing card named above and belongs to the separate row.
