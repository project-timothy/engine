# Lessons

What the engine's incidents taught, one rule per failure pattern. The rule is
the durable part; each one is enforced by an eval that was written failing
before its fix merged (invariant 8), and the eval named beside it is where the
case lives now. Particulars (which document, which night, which vendor) stay
with the business that had them.

A new incident adds a lesson here, or extends an existing one, in the same PR
as its eval and its fix.

## Oversize inputs fail fast

A transport with a fixed message buffer is a size limit whether or not anyone
chose it. The extractor enforces an explicit cap before any model call and
fails with a message naming the file's size and the cap; the transport buffer
is then sized above the cap with headroom. Eval:
`core/agents/ap/evals/test_extraction_size_cap.py`.

## Identity survives cosmetic formatting

Two records of the same invoice must join even when one side carries a
trailing parenthetical alias on the vendor name. Canonicalize the join key on
both sides; keep a name that is entirely a parenthetical distinct, so
placeholders never collapse together. Eval: `tests/unit/test_shadow_diff.py`.

## A declared bound is an enforced bound

A timeout that is accepted and stored but never applied is dead code, and one
stalled call then blocks the whole run with no ceiling. Every bound a
component declares is enforced where the call is made, and its expiry fails
that one item, not the run. Eval:
`core/agents/ap/evals/test_extraction_timeout.py`.

## Transient is not terminal

The first failed attempt is not a verdict about the document. Failures carry a
cause and a transient flag; transient causes are redialed with backoff and
terminal ones fail at once, and the cause is written to the ledger so a drop
is diagnosable from the ledger alone. Evals:
`core/agents/ap/evals/test_extraction_retry.py`,
`core/agents/ap/evals/test_intake_transport_retry.py`.

## The sweep watches where intake never looks

Intake works its own window of the landing folder. A misfiled invoice hides in
the places intake structurally never enters, so a separate sweep covers
exactly those subfolders, and nothing else, so its signal is not drowned by a
backlog intake already owns. Eval: `tests/unit/test_sweep.py`.

## Code filters what code can decide

An inline email graphic is never an invoice. When a deterministic rule can
decide, it runs before the model call, so the model is never asked a question
code already answers (invariant 2). The rule is narrow by construction: image
only and name-shaped, so a real scanned invoice never matches. And a rule that
decides from a filename decides about a name the engine itself may have
written: a save into a name already taken carries the placement contract's
collision suffix, so the rule strips that suffix through the module that
appends it instead of re-spelling the convention, and the second copy of one
file can never get a different disposition from the first. Evals:
`core/agents/ap/evals/test_inline_graphic_prefilter.py`,
`tests/unit/test_fileops.py`.

## A cloud placeholder is not here yet

A file-sync placeholder read from a headless session fails with a specific
errno. That errno means "come back later", never "this run is over": the
item is deferred with an event and an anomaly, its identity key carries a
sentinel so the run re-fires the moment the file materializes, and every other
read error still propagates. The same disease will reach every new reader of a
synced tree, so every reader gets the same seam. Evals:
`core/agents/ap/evals/test_cloud_only_deferral.py`, `auditor/evals/test_context.py`.

## Read back every write

An API can accept a request and silently ignore a field. A write is proven
only by reading back the value and comparing; a no-op write (setting the value
already there) proves nothing about the write path. Where the API cannot make
the change, the owner makes it in the UI and the engine verifies and records.
Evals: `core/agents/close/evals/test_lock.py`.

## Check the known cures before diagnosing fresh

Most new failures are an old disease in a new organ. Before diagnosing from
scratch, search this file for the failure's signature and port the pattern
that already fixed it.

## Scheduled runs execute a deploy clone

A schedule that runs the development checkout runs whatever branch or
half-finished edit a session left there. The schedule runs a dedicated deploy
clone; a preflight refuses an off-main, dirty or non-fast-forwardable clone,
pulls when cleanly behind, degrades with a warning when the remote is
unreachable, and every refusal is a checklist item the next morning. Evals:
`tests/unit/test_run_preflight.py`, `auditor/evals/test_preflight_markers.py`.

## Fixtures are captured, not transcribed

A fixture that encodes the author's expectation of an external format proves
only that the code agrees with the author. Parse fixtures are samples captured
from the live tool, and every new lens gets one live run in the state it
should call healthy, not only the state it should flag. Evals:
`auditor/evals/test_host.py`, `core/agents/close/evals/test_preflight.py`.

## Installers change only what changed, in order

Reloading a job is not free, so an installer leaves an unchanged, loaded job
alone and retries a failed load once. It puts every program in place before it
loads the first job that runs one, and it backs up a differing file before it
replaces it. A generated file is never edited by hand: change the source and
regenerate.

## Anchor windows to the domain, and freeze the clock

A matching window bounded by an event that follows the thing being matched can
never match. Bound it by what the domain guarantees; consume evidence once it
has matched; and never let a fixture's validity depend on the date the suite
runs. Eval: `core/agents/expenses/evals/test_report_match.py`.

## The strongest discriminator ranks first

A guard that outranks a stronger identity signal suppresses the very evidence
that would have decided correctly. In reconcile the evidence's own check
number resolves first; an absent discriminator degrades to the weaker rule,
and a conflicting one never agrees. Eval: `core/agents/ap/evals/test_reconcile.py`.

## A reject means ask again, and a summary reports only what happened

A dedup key shaped like an identity (the month) rather than a fingerprint
turns one resolved card into a permanent block on every later ask. A re-ask
rides a fresh key naming the card it supersedes, and a deduped enqueue against
a resolved card is an event, never silence. No job summary asserts an action
whose return value it did not read. And a record with no reader is silence
too: the dropped ask is reported on the surface its owner reads, one item per
ask however often it repeats, ageing out when the asking stops — an empty
queue is exactly what a swallowed ask looks like from every other angle.
A lane whose unanswered asks NEST (each night's set contains the last) declares the older cards it replaces and the runner closes them `superseded`, by containment and never recency, so the queue holds one question, not N. Evals: `core/agents/close/evals/test_lock.py`,
`tests/unit/test_runner_approval_swallow.py`, `auditor/evals/test_approvals.py`,
`tests/unit/test_runner_approval_supersede.py`, `core/agents/expenses/evals/test_inbox_skip_supersede.py`.

## Pin both halves of a pair

A pinned SDK driving an unpinned CLI that updates itself will one morning stop
understanding it. Pin the pair, prove the floor in a test, and let updates be
an owner's act. Evals: `tests/unit/test_sdk_cli_pair.py`,
`tests/unit/test_claude_sdk_complete_adapter.py`.

## Headless means no prompts, and a wall-clock budget

An unattended session that meets a consent prompt blocks forever and looks
like one still working. Unattended work reads only from places no prompt
gates, runs under a wall-clock budget that kills it and says why, and treats a
note whose first line says "preliminary" as unfinished.

## A denied read is not an empty read

An OS privacy gate can allow a stat by name and refuse the listing, so a guard
passes and the glob returns nothing, silently. A listing that can be refused
runs through the interpreter that holds the grant, and "refused", "empty" and
"found" are three different answers with three different exits. Eval:
`core/agents/ap/evals/test_reconcile_statement_folder.py`.

## Unattended jobs do not run through a GUI launch sequence

A helper whose launch path contains a human-facing fallback screen will, one
launch in a hundred, show it to nobody. Rebuilding or relaunching it is a new
roll of the dice, not a fix: the durable fix removes the GUI launch sequence
from the unattended path entirely. When a dialog you turned off keeps
appearing, diff the launch that showed it against one that did not, in the
system log, before touching the binary.

## Done means the goal is met

A maintenance job that defines success as "the tool I called did not error"
reports a partial outcome as a clean one. The job re-checks the condition it
exists to fix and fails loudly, with the shortfall, when it is still unmet.

## Budget for the hardest input

A turn bound tuned on easy documents turns a whole class of hard ones into
permanent review items, and a flag that reads as a transport error invites a
redial that cannot work. Measure the bound against the hardest real input, and
give the model the text layer beside the file so it can stop reading and
answer. Eval: `core/agents/ap/evals/test_extraction_turn_budget.py`.

## One environment for the whole schedule

`uv run` syncs before it runs, so the dependency set is a property of the
environment, not of the job. Every scheduled call asks for the identical set
or trusts the environment with `--no-sync`; two jobs asking for different sets
on one venv are not independent, and the loser fails on its own schedule, far
from the change. Eval: `tests/unit/test_scheduled_scripts_linux.py`.

## An improved rule must be able to retract

When a detector's evidence is an append-only log of past verdicts, a rule that
improves cannot withdraw what the old rule wrote, and the tail can only be
silenced by hand. The auditor re-derives from ledger state rather than
trusting a verdict log, and when the owner can write the root cause into a
mute, the mute is a missing join. A lane that answers its own findings owes
the detector one join per way it can answer: where the lane records the
outcome in ledger state, the detector reads that state and the finding closes
itself, so counting the hand mutes on a condition measures the joins still
missing. The ways include the lanes that never raised the finding: where one
instrument reaches the owner through several lanes and the engine suppresses
the duplicate ask, the detector joins on the identity the engine suppressed
by, or it stays loud about exactly the item that was answered best. An
identity re-spelled across an independence boundary is pinned to its original
by a parity test on the side allowed to import both. A pointer built on a
counter that only grows has the same defect one layer up and must still be
able to stop pointing: where a detector reports a repeat from a cumulative
count, the report is bounded by a window on the last sighting, so the repeat
that stops repeating ages off the checklist by itself instead of needing a
mute on the night its automation finally ships. Evals:
`auditor/evals/test_reconcile_lens.py`,
`tests/unit/test_auditor_check_ref_parity.py`,
`auditor/evals/test_recurrence.py`.

## Facts cross into prose positively

A fact computed correctly can still invert when a model paraphrases it, if it
is spelled as a negation. Facts handed to a drafter are named positively and
carried with a sentence the drafter can quote, and the drafter's rules name the
one field that may source each claim. Eval: `auditor/evals/test_advisory.py`.

## A finding explains only what it measured

An explanation stapled unconditionally to an alarm will one night explain the
opposite of what happened: the reassurance that a shortfall is self-clearing
housekeeping holds only while the reclaimable slack exists, and the night it
is gone is the night the reader most needs the alarm. Every clause that rests
on an observation is emitted only when that observation holds, and a quantity
compared against a threshold is rounded away from it, so a finding never
prints a number its own threshold says could not have raised it. An absent
alarm is an observation about the alarm's own rule and nothing wider: where a
list is deliberately narrowed to the items worth nagging about, an empty list
is reported as the narrow fact it is, never as the broad reassurance it
resembles, and the deliberately excluded items are counted beside it so
suppressing the nag never becomes suppressing the number. The same rule
governs a job summary: where several independent criteria can select the work,
the summary names the criterion each item actually met and counts only what it
did, so it never cites a cutoff nothing crossed, and a sentence with no
observation behind it is not printed at all. And a count is an observation with
a time on it: where a detector reads state its own run will then change, the
count it prints names the vintage it was read from rather than asserting the
present, so a report can never contradict its own resolved list. Evals:
`auditor/evals/test_host.py`, `core/agents/ap/evals/test_janitor.py`,
`auditor/evals/test_recurrence.py`, `auditor/evals/test_advisory.py`.

## Two lists that must agree are pinned against each other

A monitoring list maintained by hand beside the thing it monitors narrows
silently every time the thing grows, and an unwatched stage reads exactly like
a healthy one. A test executes or parses the real list and fails on any entry
the monitor does not name; the comparison is one-directional where the
converse is legitimate.

## A photograph taken once a day lies the rest of it (2026-10-04, #326)

The AP workbook rendered once a morning, and every owner act after the shutter
left it wrong until the next one; on 2026-09-17 six writes left it wrong about
$91,700 for twenty hours and the auditor, correctly, raised six CRITICALs.
The render already knew when it was stale (its key); nothing asked it. The
trigger moved to the store, the one place every write passes, including the
hand backfills no run-level hook can see. A delivered view is refreshed by the
write that changed it, never by the calendar.

