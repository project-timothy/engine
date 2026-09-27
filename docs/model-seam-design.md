# The model seam: `core/llm`

Phase 7 row 7.8.
Decision record: `docs/decisions/2026-09-11-model-gateway-interface.md`
(one-way door: the interface is what every later adapter targets).

## Why a seam

Today the engine has six model call sites in four modules
(`core/agents/ap/extraction.py` twice, `core/agents/expenses/inbox.py`,
`core/agents/expenses/scan_split.py`, `auditor/advisory/draft.py`). Three of
the four are Claude-only through the Agent SDK, which spawns the Claude Code
binary and rides a Max seat. A product tenant cannot ship on that: the
provider has to be a tenant setting, and the money and validation rules have
to live in one place instead of four. This row ships the gateway with no
callers; rows 7.9 to 7.12 wire the sites over one at a time.

## The interface

```python
from core.llm import complete, Message, Attachment, DecimalString

result = complete(
    "invoice_extract",              # job_type: the policy key, the telemetry key
    [Message("system", brief), Message("user", ask)],
    ExtractedDocument,              # a pydantic model: the reply contract
    adapter=adapter,                # AnthropicMessagesAdapter | OpenAICompatAdapter | FixtureAdapter
    model="<resolved by the tenant policy>",
    attachments=[Attachment(path, "application/pdf")],
    timeout_s=120,
    pricing=None,                   # Pricing from the policy; None = usd not computed
)
result.output   # the validated ExtractedDocument
result.record   # CallRecord: what it cost
```

`Message` is `(role, content)` with `role` in `system | user | assistant`.
`Attachment` is `(path, mime)`; the adapter reads the bytes at call time and
inlines them base64 on the wire (images and PDFs). Attachments ride the
FIRST user turn (the ask), so they stay in place when the retry appends the
correction turns.

`Adapter` is a Protocol with one method:

```python
def complete(self, bundle: PromptBundle, schema: dict) -> RawReply: ...
```

`PromptBundle` carries `job_type, model, messages, attachments, timeout_s`;
`RawReply` carries `text, usage (input_tokens, output_tokens), model` (the
id the provider reports back, or None). An adapter raises anything it likes
on a transport failure; the gateway maps it.

The gateway raises exactly three things: `GatewaySchemaError` (the output
model breaks a seam rule, raised before any call), `GatewayTransportError`
(`cause` in `timeout | transport_error | no_api_key | refusal`, plus
`transient`, the same taxonomy `ExtractionError` already uses so the AP retry
wrapper can keep redialing transient failures), and `GatewayValidationError`.
The gateway does not retry transport failures; that stays with the caller
(`RetryingExtractor` today), one policy per job.

## The structured-output rule

1. The JSON schema is `output_model.model_json_schema()`.
2. It goes to the adapter as a structured argument. A provider with
   constrained decoding uses it structurally (Anthropic: `output_config.format`
   with `{"type": "json_schema", "schema": ...}`, no beta header as of the
   2026-09 docs). A provider without one gets JSON mode
   (`response_format={"type": "json_object"}`).
3. It ALSO goes into a system turn as text on every call, so a model with no
   schema slot still sees the shape, and a model with one sees it twice.
4. Pydantic validates the reply regardless of what the provider promised. The
   first `{...}` in the reply is parsed (prose or a code fence around it is
   tolerated, as `_extract_json_reply` tolerates today).
5. A reply that fails gets ONE retry: the failed reply goes back as the
   assistant turn, the validation error text as the next user turn.
6. A second failure raises `GatewayValidationError` carrying `job_type`, both
   raw replies, and both error texts. The caller turns that into a review
   card (the PRD's "then a review card"); the gateway never decides that.

Constrained decoding on the Anthropic side accepts a narrower schema than
pydantic emits: every object needs `additionalProperties: false`, and numeric
or length bounds (`minimum`, `maximum`, `minLength`, `maxLength`, ...) are
rejected. `constrained_schema()` in the Anthropic adapter tightens the schema
on the way out; pydantic still enforces the bounds on the way back. [NEEDS
REVIEW] `pattern` is neither listed as supported nor unsupported in the docs
and is passed through; if a live call returns 400 on it, add it to the strip
list.

The top-level output is always an object. A job whose natural reply is a
list (the scan grouper) wraps it: `class Groups(BaseModel): groups: list[...]`.

## The money rule

Money fields are `DecimalString` in every output schema: a `str` whose
validator checks `Decimal(value)` parses and is finite. A JSON number never
validates into it (pydantic does not coerce numbers to `str`), so a model
that answers `1875.0` fails validation and gets the retry, then the error.
The caller re-parses with `Decimal(...)` in code, where the arithmetic lives
(invariant 2: no LLM output writes a money value directly).

An output model that declares a field as `Decimal` (anywhere, nested models
included) is refused with `GatewaySchemaError` before any call is made,
because pydantic would coerce a JSON float into it silently. The gateway
never returns a float for money; the test suite pins both halves.

## The policy table (row 7.9, implemented)

```toml
[llm.tiers]
cheap    = { adapter = "openai_compat", model = "<id>", base_url = "<endpoint>", api_key_env = "<VAR>", pricing = { input_usd_per_mtok = "0.10", output_usd_per_mtok = "0.40" }, fallback = ["standard"] }
standard = { adapter = "anthropic_messages", model = "<id>", api_key_env = "<VAR>", pricing = { input_usd_per_mtok = "3", output_usd_per_mtok = "15" } }

[llm.jobs]
inbox_classify  = "cheap"
receipt_extract = "standard"
invoice_extract = "standard"
scan_group      = "standard"
w9_detect       = "deterministic"     # no model may be called for this job
draft_advisory  = "standard"
audit_triage    = "standard"
default         = "standard"          # optional: the tier for any job not listed

[llm.budget]
monthly_usd = 25
```

`core/engine/config.py` parses the three tables (`LlmSettings`) and checks
every reference at load: a job naming an unknown tier, a tier named
`deterministic`, a fallback that is not another tier, or an unknown adapter
name fails config validation naming the offender. `core/llm/policy.py` does
the rest: `resolve(settings, job_type)` picks the tier from `[llm.jobs]`
(then `default`, else `UnresolvedJobError`; a `deterministic` job raises
`DeterministicJobError`), and `complete_for(ctx, job_type, messages,
output_model, *, attachments, timeout_s, adapter, now)` wraps `complete()`
with the signature above untouched: it attaches the tier's `Pricing`, walks
the tier's `fallback` list (other tier names) on a transient transport
failure, writes one `llm_calls` row per gateway call through
`core/llm/telemetry.py`, and refuses a call once the tenant's month-to-date
`usd` has reached `monthly_usd` (a `budget_refused` row, a typed
`BudgetExceeded` the job catches, an `llm.budget` anomaly on the run).
Provider connection details (endpoint, key environment variable NAME) live
in the tier; the key VALUE is resolved from the environment at call time
and never enters a tenant file or the repo. The choices the plan did not
specify are in `docs/decisions/2026-09-12-llm-policy-table-shape.md`.

The `claude_agent_sdk` adapter name was legal-but-unbuildable in row 7.9 so a
tenant could state what it runs today (the SDK under a seat) before its sites
moved. Row 7.10 shipped the adapter, so that tier now SERVES a job at the same
flat rate. `complete_for` also takes `tier=<name>`, which pins a tier instead
of resolving the job type (the AP extractor's `--param extractor=` aliases); a
job the table calls `deterministic` is refused even with a tier in hand.

## Telemetry: `CallRecord`

Every successful call returns one record, summed over its attempts:

| field | meaning |
|---|---|
| `job_type` | the policy key |
| `adapter` | `fixture`, `anthropic_messages`, `openai_compat`, `claude_agent_sdk` |
| `model` | the id the caller asked for |
| `provider_model` | the id the provider reported back, if any |
| `input_tokens`, `output_tokens` | as the provider reported them |
| `usd` | `Decimal`, from `Pricing`; `None` when no pricing was given |
| `retries` | 0 or 1 |
| `latency_ms` | wall clock across attempts |

The gateway never writes it anywhere. `complete_for` (row 7.9) persists it
as an `llm_calls` row (plus `tier`, `status`, `detail`), and the runner sums
a run's rows onto `RunResult.llm` (`calls`, `tokens_in`, `tokens_out`,
`usd`) when the run lands.

## Adapters

- `adapters/fixture.py`: canned replies from a dict keyed by job type (`"*"`
  as default), a list handed out in order, or a callable
  `(bundle, schema) -> RawReply | str`; seeded with an exception instance it
  raises on every call. Keeps every bundle and schema on `.calls` and
  `.schemas`. This is the CI adapter: every test and every eval runs against
  it, so the suite is offline and needs no secret. The existing sidecar
  fixtures (`.extract.json`, `.label.json`, `.groups.json`) become fixture
  replies when the sites move (rows 7.10, 7.11).
- `adapters/anthropic_messages.py`: the Messages API over stdlib `urllib`
  (no new dependency). API key from the environment variable NAMED in the
  constructor. Images as `image` blocks, PDFs as `document` blocks, system
  turns folded into the top-level `system` field, temperature 0. A
  `stop_reason` of `refusal` raises a non-transient transport error. A PDF
  with NO text layer never travels as a document block: the engine renders it
  to page images first (below) and they ride as `image` blocks.
- `adapters/openai_compat.py`: the pinned `openai` client exactly as the AP
  gateway extractor uses it today (`base_url`, key env var with the
  `sk-no-auth` placeholder for the local gateway, JSON mode, temperature 0).
  Covers OpenAI, the Gemini compatibility endpoint, OpenRouter, vLLM, Ollama,
  and the existing local gateway. Images as `image_url` data URLs; a PDF as
  the `file` content part (a compatibility endpoint without it returns an
  error the gateway maps to a transport failure; the caller's text-extraction
  path stays the fallback). A PDF with no text layer is rendered to page
  images first (below), which is what makes a scan extractable on a local
  model with no PDF reader of its own.
- `adapters/claude_sdk_complete.py` (row 7.10): the Claude Agent SDK as a
  `complete()` adapter, so the tier describing the owner's seat can serve a
  job. One `query()` per call with the extractor's own settings (12 turns, a
  32 MB transport buffer); `system_text()` and the turns fold into the one
  prompt string, and each attachment becomes a "Read the document at <path>"
  line (the SDK reads files off disk, so nothing is base64 inlined). A tier
  whose `model` is `default` (or empty) leaves the option unset so the CLI's
  login decides. The SDK is the `[claude]` extra: absent, the adapter raises a
  NON-transient `sdk_missing` transport failure naming the extra, never a bare
  ImportError.

  **The seat session is Read-only, and this time the code enforces it** (issue
  #296). It was not, for a real reason: `allowed_tools` is a PRE-APPROVAL list
  in the Claude Agent SDK, not a restriction, so a tool left off it is
  unapproved rather than refused, and the sessions of 2026-09-16 used that to
  rasterize image-only PDFs with `pdftoppm` under Bash and read the pages
  back. Issue #265 kept the ability and wrote it down honestly; this row took
  the reason away instead. The ENGINE renders the pages now
  (`core/llm/rasterize.py`), so:

  - `allowed_tools` is `["Read"]`, and only when the bundle carries an
    attachment (a text-only call still gets no tools at all);
  - `disallowed_tools` names Bash, BashOutput, KillShell, Write, Edit,
    NotebookEdit, WebFetch, WebSearch and Task. The SDK removes a disallowed
    tool from the session's context outright, which is the difference between
    a confinement and a wish. A `can_use_tool` callback would not do it: the
    SDK warns that a whole-tool `allowed_tools` entry auto-approves before any
    callback is consulted;
  - every call with an attachment still gets its own directory under the
    engine's state root, resolved the way the ledger root is
    (`ENGINE_LLM_SCRATCH_ROOT`, else `$ENGINE_DATA_ROOT/llm-scratch` on a
    container, else `.llm-scratch` beside the run), owner-only and named for
    the job it serves. The rendered pages go in it as `pages/page-1.png`
    upward, and the prompt sends the session to those instead of to the PDF;
  - when the call returns, by any exit, the directory is inventoried (each
    relative path and its bytes) and swept, and the record is appended to
    `scratch-log.jsonl` under the root, with the last one on the adapter as
    `last_scratch`. The pages are copies of a document the filing tree already
    holds, so the inventory is what is worth keeping; a `llm_calls` column for
    the path would be a ledger schema migration, which is the owner's one-way
    door.

  The other adapters have no tools at all: `anthropic_messages` and
  `openai_compat` put the pages (or the document) on the wire and nothing
  executes anywhere.
- `rasterize.py` (row 7.10, issue #296): page images for a document no model
  can read as a file. `has_text_layer` asks `pypdf`, the same reader the AP
  extractor inlines the text layer with, so nothing renders that could simply
  be read. When there is no text layer, `pypdfium2` renders up to 6 pages at
  150 DPI and up to 12 MB of PNG, whichever comes first, and the cut is named
  in a line of prose the model reads with the pictures. The PNG is encoded
  with `zlib` and `struct`, so Pillow is not a second dependency. A render
  that cannot happen (encrypted, malformed, missing, no renderer installed) is
  not a failure: the file goes to the model exactly as it did before and comes
  back `needs_ocr` if nobody can read it. The dependency is the owner's
  one-way door, taken on 2026-09-17:
  `docs/decisions/2026-09-17-rasterize-image-only-pdfs-in-python.md`.
- `adapters/claude_sdk.py` is a different thing with a similar name: the
  agentic RUNNER for the skill lanes (row 7.16), not a `complete()` adapter.

No model name is hard-coded anywhere under `core/llm`; a test greps for the
family literals and fails on a hit. Models are tenant policy.

## The auditor independence rule

`auditor/` imports nothing from `core/` (CI-enforced by
`auditor.evals.independence_lint`). `auditor/advisory/draft.py` therefore
cannot import this package. Row 7.12 vendors a minimal copy of the interface
under `auditor/`, kept in step by a parity test the way the auditor's ledger
reader is today. This note is the contract both copies follow.

Landed 2026-09-15 as `auditor/advisory/llm_client.py`: `resolve(llm, job)`
over the tenant's own `[llm.jobs]` / `[llm.tiers]` (read by the auditor's TOML
reader, `AuditorTenantConfig.raw`), a `Prompt` of one system turn and one
facts turn, and a client per adapter name (`anthropic_messages` and
`openai_compat` over stdlib `urllib`, `claude_agent_sdk` for the seat path the
drafter always ran, `fixture` for evals). What it deliberately does NOT copy:
the pydantic output model, `DecimalString`, the validation retry, and the tier
`fallback` walk. The advisory reply is PROSE, one string in one report section,
so there is no schema to validate and no money field to protect; a failure is
answered by the deterministic `fallback_counsel` rather than by a second model,
and the report prints one line naming the fallback voice and a short reason
label. The parity tripwire is `tests/unit/test_auditor_advisory_gateway.py`
(adapter names, the `deterministic` word, the `default` job key, the Anthropic
endpoint and version, the no-auth placeholder, the tier's field set). Decision:
`docs/decisions/2026-09-15-advisory-drafter-vendored-seam.md`.

## The runner (row 7.16)

The agentic lanes sit on top of this seam, not inside it: `docs/runner-design.md`
(decision `docs/decisions/2026-09-12-runner-contract.md`) defines
`core.llm.runner.run(skill, context, allowlist, *, runner, budget)`. Its
in-repo loop is a caller of `complete()`, one turn per call against a `Turn`
output model; its Claude Agent SDK runner is the 7.15 extra. Both sum this
note's `CallRecord` for tokens and usd. Nothing in `complete()` changes for it.

## The eval gate (row 7.13, implemented)

The seam made the model a tenant setting, so one line in `tenant.toml` can
repoint the 08:00 run at a model nobody has tested. The gate makes the
evidence a precondition of the setting.

Each GATED job owns a directory beside the harness:

```
core/llm/eval_sets/<job>/
    job.json            the reply contract (module:attr) + the probe prompt
    cases/*.json        one case: a note, the document text, the expected fields
    documents/*.pdf     what the case attaches (conftest.minimal_pdf of the text)
    results/<id>.json   one model's score, by model id (slugged for a filename)
```

`core/engine/config.py` calls `check_eval_gate` at the end of `load_tenant`.
For every `[llm.jobs]` assignment whose job is gated, the tier's model must
have `results/<model_id>.json`, that file must report `failed == 0`, and it
must have been scored on the tier's own adapter. Missing, unreadable, red, or
scored elsewhere are four refusals with one command in the message:

```
[llm.jobs].invoice_extract = "cheap" runs model 'some-id' on adapter
'openai_compat', which has no eval results for this job. Run the set and
commit the results file:
  uv run engine evals run invoice_extract --tenant <slug> --tier cheap
expected at core/llm/eval_sets/invoice_extract/results/some-id.json
```

Three things are never gated: a job with no `cases/` directory (gating is
additive, and writing the set is what turns it on), a tier no job uses, and a
job the tenant calls `deterministic`. Today's gated jobs are
`invoice_extract`, `receipt_extract`, `inbox_classify`, and `scan_group`,
which joined them in row 7.13b once 7.11 landed the grouper's reply contract;
`draft_advisory` belongs to the auditor's vendored copy (independence), and
`audit_triage` is a runner lane with no single reply schema. That is every
`complete_for` site in the engine.

`scan_group` is the first gated contract holding a LIST of objects, and it
set two harness rules. An expectation is a subset all the way down, so a case
states the grouping and says nothing about the vendor, amount, and date that
only ever name a child file; and `{page_count}` in the `user` template is the
pages of the case's own document, so the probe can ask what the live site asks
("the attached 4-page scan"). What a case still cannot state is a REFUSAL: a
grouping that lists a page twice or drops one is rejected by
`validate_groups`, in code, after the reply validates, and that half is tested
where the rule lives (`tests/unit/test_scan_group_eval_set.py`). Decision:
`docs/decisions/2026-09-16-a-case-states-the-part-under-test.md`.

The commands:

```
uv run engine evals list                                   # sets, case counts, who has been scored
uv run engine evals run <job> --tenant <slug> --tier <name>  # a real model, through the tier
uv run engine evals run <job> --model <id>                 # the fixture adapter, seeded from the cases
```

`engine evals run` loads the tenant with the gate OFF (`check_evals=False`):
the command that produces the evidence has to read the file that lacks it.
It exits non-zero when a case fails, and writes the results file either way.
A seeded fixture run scores the HARNESS, not a model, so it is marked
`seeded` and the gate accepts it for a fixture tier only. The harness writes
no `llm_calls` row and touches no ledger: an eval run is a measurement, not a
job, and the results file is deliberately not a run-key input
(`docs/run-keys.md`). Decision:
`docs/decisions/2026-09-16-eval-gated-tier-changes.md`.

## The call sites

- **AP extraction (row 7.10, done).** `core/agents/ap/extraction.py` holds one
  `GatewayExtractor`; the two provider classes are gone.
  `[llm.jobs].invoice_extract` serves AP intake and `receipt_extract` the
  expenses pass. The reply contract is `ExtractionReply`, field for field
  `ExtractedDocument` with `amount` as a `DecimalString`, re-parsed to a
  `Decimal` in code. Every call is one `llm_calls` row, and both run keys fold
  in `[llm]` plus the resolved `(tier, adapter, model)`. The extractor attaches
  the file AND inlines its text layer when there is one, one prompt for every
  provider. Decision:
  `docs/decisions/2026-09-15-ap-extraction-through-the-gateway.md`.
- **The two expenses sites (row 7.11, done).** `GatewayInboxClassifier`
  (`core/agents/expenses/inbox.py`, `[llm.jobs].inbox_classify`) and
  `GatewayGrouper` (`core/agents/expenses/scan_split.py`,
  `[llm.jobs].scan_group`) replaced their SDK classes; the fixture
  classifier and grouper stay. What did NOT move is the code that decides:
  `sanitize()` still strips a non-receipt label to the boolean plus the
  confidence, and `_parse_groups_payload` plus `validate_groups` still refuse
  a page map that does not cover every page exactly once. The grouper's
  natural reply is a list, so it rides in the object `GroupingReply`
  (`{"groups": [...]}`). The `expenses/inbox` and `expenses/intake` run keys
  fold in `[llm]` plus the resolved `(tier, adapter, model)`. The classifier
  passes `redact_detail=True`, so a validation error (which quotes the model's
  reply) never puts photo content in `llm_calls`. Decision:
  `docs/decisions/2026-09-16-expenses-sites-through-the-gateway.md`.
- **The auditor's own copy (row 7.12, done):** see "The auditor independence rule"
  above.
- **Still to move:** nothing. Every model call site in the engine and the
  auditor now runs through this seam. Eval sets per job and eval-gated tier
  changes landed as 7.13 (above); the agentic runner is 7.16
  (`docs/runner-design.md`).

2026-09-12: the SDK as an optional extra (7.15) landed: `claude-agent-sdk` is
the `[claude]` extra, `core/llm/sdk.py` is the lazy import seam every SDK site
uses (`SdkMissing`, a `ModuleNotFoundError` naming the extra), and CI runs the
suite once with the extra and once without.
