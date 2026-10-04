# The two expenses model sites run through the gateway, and a failure detail can be withheld
Date: 2026-09-16
Type: Two-way door (phase 7 row 7.11, issue #220; follows 2026-09-15-ap-extraction-through-the-gateway)

`ClaudeInboxClassifier` and `ClaudeGrouper` each held a contract against one
hard-coded provider, with their own turn bounds and their own transport
buffers. They are `GatewayInboxClassifier` and `GatewayGrouper` now, and which
model answers is `[llm.tiers]` plus `[llm.jobs].inbox_classify` and
`[llm.jobs].scan_group`. `FixtureInboxClassifier` and `FixtureGrouper` are
unchanged.

What did NOT move is the code that decides. `sanitize()` still strips a
non-receipt label to the boolean plus the confidence, whatever a model says,
and it is applied to every gateway reply. `_parse_groups_payload` still parses
the proposal and `validate_groups` still refuses a page map that does not cover
every page exactly once, so a scan the model groups badly is held and nothing
is filed.

Five choices the row did not specify.

## 1. A failure detail can be withheld, because a validation error quotes the reply

`complete_for` records the failure text on the `llm_calls` row, and a pydantic
validation error carries `input_value=...`: the model's own words about the
image. For the receipt inbox that is the label-only contract's exact failure
mode (the owner's 2026-08-12 decision: a non-receipt's content never reaches a
card, an event, or a log), arriving through the telemetry table nobody was
watching for it. Measured, not assumed: a reply of
`{"receipt": "no, this is <a description>"}` puts that description in the
error string, and pydantic truncates only past about 50 characters.

So `complete_for` gained `redact_detail`, default `False`, and the classifier
passes `True`. The row still records that the call failed and which failure it
was (the `status` column is untouched); only the text is replaced with a fixed
sentence. The grouper does NOT set it: a grouping reply describes receipts, its
failure text is real diagnostic value, and nothing in it is private in the way a
personal photo is.

## 2. The natural reply is a list, so it rides inside an object

The seam's top-level output is always an object. The grouper's reply is a list
of groups, so it wraps: `GroupingReply{groups: list[GroupReply]}`, exactly as
`docs/model-seam-design.md` anticipated for this site. The parsed groups then go
back through `_parse_groups_payload`, which keeps the parse (and its error
messages) in the code that owns it rather than letting pydantic quietly become
the only gate.

## 3. Failure policy: is this about the file, or about the deployment?

The grouper converts anything about THIS SCAN into a `GroupingError` (a
transport blip, a reply the engine cannot use, a spent monthly budget): the scan
is held in the drop tree with an anomaly, nothing is filed, and the rest of
intake still runs. Anything about the DEPLOYMENT escapes the split pass
unchanged: a missing `[claude]` extra and a policy gap fail the job loudly with
the #172 failure trace, because holding every scan in the tree would hide a
host that lost a package.

The classifier is deliberately the other way around on everything but an
unusable reply. Its answer becomes EVENT MEMORY (one model look per image,
ever), so a failure that produced a wrong label would be remembered and never
re-asked. An unusable reply answers "not a receipt" with no confidence, exactly
as before the gateway, and the image waits for a human on a skip card;
everything else propagates. A budget refusal therefore fails the inbox job
rather than labelling ten photos "not a receipt" for good.

## 4. The aliases are `fixture`, `claude`, and `tier:<name>`, resolved in one place

`--param classifier=` and `--param grouper=` keep `claude` for one release,
meaning "the tier the policy names for this job"; `fixture` needs no policy at
all; `tier:<name>` says it directly. Neither site ever had a `qwen` alias, so
neither gained one.

The resolution itself moved into `core/llm/policy.py` (`tier_for_alias`,
`resolved_tier_for_alias`), which is the module whose docstring claims to be the
only place `[llm.tiers]` and `[llm.jobs]` are read. Row 7.10's copy in
`core/agents/ap/extraction.py` is left alone on purpose: it carries the `qwen`
alias's fall-through rule, it landed yesterday with its own evals, and folding
it onto the shared helper is a rebase risk this row does not need. Two
implementations of the same idea is a smell; the follow-up is small and belongs
to a day that is not also moving two call sites.

## 5. The run keys, and the job that was documented in the wrong place

`expenses/inbox` and `expenses/intake` now declare `[llm]` and fold the resolved
`(tier, adapter, model)`, so repointing a job at another tier re-classifies or
re-groups instead of replaying a result the previous model produced
(`docs/run-keys.md`). `expenses/extract` already folded `receipt_extract` and
needed nothing: the split pass runs in INTAKE, not extract, which the tenant
file's own comment had wrong since row 7.9. The comment is corrected; the value
is unchanged.

## What merging does

It changes the 08:00 run's model path for the two expenses sites, so the
owner's coding-day rule applies. Both run keys change, so `expenses/inbox` and
`expenses/intake` execute once after the merge instead of replaying. Both are
bookkeeping-neutral on re-execution by construction: classification is event
memory keyed by content hash (an image already classified is never re-asked, and
the re-execution re-reads the event), and the split pass skips any scan that
already carries a verdict. On the first tenant, day one is the same seat
through the same SDK adapter at zero metered cost, now with one `llm_calls` row
per image and per scan.
