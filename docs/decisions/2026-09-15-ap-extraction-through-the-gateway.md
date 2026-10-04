# AP extraction runs through the gateway, and the seat becomes a real adapter
Date: 2026-09-15
Type: Two-way door (phase 7 row 7.10, issue #219; refines 2026-09-12-llm-policy-table-shape)

`ClaudeExtractor` and `QwenExtractor` were two classes holding one contract
with two hard-coded providers. They are one `GatewayExtractor` now, and which
model answers is `[llm.tiers]` plus `[llm.jobs].invoice_extract`
(`receipt_extract` for the expenses pass). `FixtureExtractor` and
`RetryingExtractor` are unchanged. The W-9 deterministic pre-model detection is
untouched: a detected form is routed away before any extractor is asked, so no
TIN reaches a model whatever tier serves the job.

Four choices the row did not specify, and a fifth the row got wrong and the
owner settled on 2026-09-17 (section 5).

## 1. The seat tier gets a real `complete()` adapter

Row 7.9 left `claude_agent_sdk` describe-only: the policy refused to build it.
Moving extraction behind the gateway with that refusal in place would have
moved the daily run onto a metered tier and changed what the owner pays, so
the row ships `core/llm/adapters/claude_sdk_complete.py`. It makes the same
call the extractor made: one `query()` with the `Read` tool and a 32 MB
transport buffer, the SDK imported lazily through `core/llm/sdk.py`. A
missing `[claude]` extra is a NON-transient `sdk_missing` transport failure, so
the retry wrapper still refuses to redial a missing package.

`model = "default"` in a tier is a SENTINEL, not a model id: it omits the
option so the CLI's own login decides, which is how the extractor called the
seat (`model=None`). An empty model string does the same.

One setting moved rather than being copied: the turn bound is 12, not 4. A
live shadow run on the real landing folder found the document today's 08:00
run had already flagged failing at 4 turns, and reproducing the pre-7.10 call
by hand raised the same error, so 4 was already too low in production
(docs/lessons.md, "Budget for the hardest input"). The number is a judgment; the direction is
evidence. `timeout_s` remains the real bound on a runaway session.

## 2. One prompt shape for every provider: attach the file, inline the text

The extractor attaches the document (PDF or image) on every call AND inlines
its text layer when the file has one. The adapter decides what to do with the
attachment: the SDK adapter turns it into a `Read`, the OpenAI-compatible
adapter inlines it as a data URL. The alternative was for the extractor to
sniff the tier's adapter and pick a prompt, which would have put provider
knowledge back in the caller the row exists to clean up.

This is also what rescued the failing document: with the file alone it
exhausted 8 turns on the live seat; with the text layer in the prompt it
extracted first try. The text is what lets the model stop reading and answer.

Two consequences, both accepted. A file with no text layer no longer
short-circuits to `needs_ocr` on the old `vision_model` knob (gone: whether a
tier can read an image is a property of the tier); the file goes to the model
and `needs_ocr` comes back as the model's own answer. And a document with both
a text layer and a readable file costs the tokens twice on a metered tier. The
seat is flat rate, so day one costs nothing; a metered tier that wants only
one of the two is a later tier-level setting, not a caller branch.

## 3. Aliases for one release, then tiers by name

`--param extractor=claude` means "the tier the policy names for this job";
`qwen` means the tenant's `local` tier when it defines one, else the policy's
own resolution; `fixture` is the sidecar extractor and needs no policy at all.
The new spelling says it directly: `tier:<name>`. Anything else raises naming
what is legal, and a `tier:` naming a tier the tenant does not define raises
naming the tiers it does. `complete_for` gained a `tier=` override to serve
this; a job the table calls `deterministic` is still refused with a tier in
hand, because that word outranks any caller.

## 4. The run key follows the resolved tier, not the alias

`ap/intake` and `expenses/extract` declare `[llm]` and fold
`(tier, adapter, model)` into the digest. Repointing
`[llm.jobs].invoice_extract` at another tier therefore re-extracts instead of
replaying a result the previous model produced, which is the 2026-07-20 replay
disease applied to model choice (`docs/run-keys.md`). Reading the text layer is
new on the default path, so a parser that raises degrades to "no text layer"
rather than taking a whole intake run down.

## 5. The seat session keeps Bash and gets a named directory (2026-09-17, issue #265)

Section 1 above says the adapter makes "one `query()` with the `Read` tool",
and the design note said the same. Both read as if the session were Read-only.
It is not, and it never was, including under the `ClaudeExtractor` this row
replaced: **`allowed_tools` is a PRE-APPROVAL list in the Claude Agent SDK, not
a restriction.** A tool left off it is unapproved rather than refused, and a
headless session's effective permission mode runs it. The live 08:00 run of
2026-09-16 is the evidence: all three extraction sessions ran `ls`, `file`, a
`pypdf` probe, and `pdftoppm -png` into a shared temporary directory, then read
the rendered pages.

The owner's call on 2026-09-17 was option (b) of the issue: **keep Bash, and
say so.** Rasterizing is the only reason an image-only PDF extracts at all;
taking the ability away would send that whole class of document to review to
buy a confinement the adapter cannot actually enforce from the caller's side.
What was wrong was not the tool, it was the silence and the destination.

So the row now:

- names `Read` and `Bash` in the pre-approval list, and only when the bundle
  carries an attachment. The list describes the session that exists;
- makes one directory per call under the engine's own state root, resolved the
  way `resolve_ledger_root` resolves the ledger's (`ENGINE_LLM_SCRATCH_ROOT`,
  then the container's `ENGINE_DATA_ROOT`, then `.llm-scratch` beside the run,
  which is in `.gitignore` for the same reason `.ledger` is: the freshness
  guard refuses a checkout carrying untracked files). The tenant's `report_dir`
  was the alternative and was rejected: nothing under `core/llm` may read a
  tenant (invariant 5), the adapter is handed a `ResolvedModel` and no paths,
  and a delivery folder that syncs to a cloud drive is the last place to put a
  model's working files;
- tells the session, in the prompt, that the directory is the only place it may
  write. That is the whole enforcement, and it is honest about being an
  instruction rather than a sandbox;
- inventories the directory and sweeps it on every exit path, records the path,
  the files and the bytes to `scratch-log.jsonl` under the root, and keeps the
  last record on the adapter. The pages are copies of a document already filed,
  so the inventory is the auditable part and the bytes are not worth keeping at
  rest. The path does not reach the `llm_calls` row: that column is a ledger
  schema migration, the owner's one-way door, and the record beside the call is
  enough to read a transcript against.

**The follow-up was done the same day (issue #296), and it takes Bash back
out.** The owner opened the dependency door on 2026-09-17
(`docs/decisions/2026-09-17-rasterize-image-only-pdfs-in-python.md`), so
`core/llm/rasterize.py` renders an image-only PDF to page images in Python and
the seat session has nothing left to shell out for: `allowed_tools` is
`["Read"]` again and `disallowed_tools` refuses Bash and everything else that
could write or run, which is a confinement rather than the gap in a
pre-approval list this section was written about. Everything above still holds
except the tool list: the directory, the prompt line, the sweep and the record
are what the rendered pages live in, and they are what makes the pages
disappear when the call ends.

Read the two together in order. The finding stands (a pre-approval list does
not restrict), the honesty stands (say what the session does), and the fix
that followed is that the session now does less.

## What merging does

(Section 5's half, merged later: the 08:00 run's extraction sessions write into
a directory the engine made and sweeps, instead of a shared temporary one, and
the pre-approval list names the tool they were already running. No run key
moves, so nothing re-executes on account of it.)

It changes the 08:00 run's model path (AP intake and the expenses extract
pass), so the owner's coding-day rule applies. Both run keys change, so intake
and extract execute once instead of replaying; both are bookkeeping-neutral on
re-execution (they dedup on content and on subject keys). On the first
tenant, day one is the seat through the new SDK adapter at zero metered cost,
now with one `llm_calls` row per document.
