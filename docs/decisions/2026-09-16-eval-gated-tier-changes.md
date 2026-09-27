# A tier change is refused at config load until the model has scored the job's eval set
Date: 2026-09-16
Type: Two-way door (phase 7 row 7.13, issue #222; builds on 2026-09-12-llm-policy-table-shape)

Rows 7.9 to 7.12 made the model a tenant SETTING. That is the point of the
seam and also its new risk: one line in `tenant.toml` can repoint the 08:00
run at a model nobody has ever tested, and the first evidence would be a
wrong invoice. This row makes the evidence a precondition. Each gated model
job owns `core/llm/eval_sets/<job>/` (a reply contract, a probe prompt, three
to five cases, and `results/<model_id>.json` per model scored), and
`load_tenant` refuses a `[llm.jobs]` assignment whose model has no green
results file, naming the exact command that produces one.

Seven choices the row did not specify.

## 1. What is gated: a job with cases, and nothing else

The rule is one sentence: **a job is gated when it owns a `cases/`
directory**. Everything else loads. A tier no job uses is not gated (nothing
runs on it). A job the tenant calls `deterministic` is not gated (no model
may serve it at all). A job with no case set is not gated, because the
alternative is a gate that refuses every tenant on the day it merges and
teaches everyone to route around it.

That makes the gate additive: writing a set is what turns it on for a job,
and the set arrives with the results that keep the shipped tenants loading.
This row ships three sets, and four jobs stay ungated with reasons:

| job | gated | why |
|---|---|---|
| `invoice_extract` | yes | 4 cases: the four house rules in the extraction brief |
| `receipt_extract` | yes | 3 cases: the category word, on the same contract |
| `inbox_classify` | yes | 3 cases, two of them the label-only contract |
| `scan_group` | no | its reply contract (`GroupingReply`) lands with row 7.11, which is not on main yet; the set is a few cases behind that merge |
| `draft_advisory` | no | it lives in the auditor's vendored client, and `auditor/` imports nothing from `core/`. Reaching into it from this harness would break the independence lint. The auditor can grow its own gate against its own copy |
| `audit_triage` | no | a runner lane (rows 7.16 to 7.18), not a `complete_for` site: a multi-turn session with tools and no single reply schema. Scoring it is the skill-eval work of row 7.14, not a case file with an expected object |
| `w9_detect` | never | `deterministic` by tenant policy |

**[NEEDS REVIEW]** the three-of-seven coverage. The rule is right; the
coverage is a starting point, and `scan_group` in particular should follow
7.11 within days.

## 2. The gate wants green results, not merely a file

The row said "no results file is refused". A file full of failures is
strictly worse than no file: it looks like evidence. So the gate refuses
three shapes with three messages: missing, unreadable, and red (`failed > 0`,
naming the count and the run date). Same command in every message.

## 3. A seeded fixture run cannot open the gate for a real adapter

The fixture adapter answers each case from the case's own `expect`, which
makes `results/fixture-model.json` a HARNESS self-test: it proves the cases
parse, the contract validates, and the checks line up. It proves nothing
about a model. That file is exactly what the demo tenant and every
`engine init` tenant need, because their tier IS the fixture adapter. It
must not be a way to fabricate evidence for a real one, so the gate compares
the results file's `adapter` with the tier's, and every seeded file carries
`"seeded": true` and says so in its own summary line.

The same reasoning sets what a bare `--model <id>` does: a model id names no
adapter, no endpoint, and no key variable, so `engine evals run <job> --model
<id>` runs the seeded fixture path and writes a file that opens the gate for
a fixture tier only. `--tier <name> --tenant <slug>` is the honest path to a
real model, because a tier is where the connection lives. Both spellings
exist: the row's acceptance names `--model`, and `--tier` is what anyone
scoring a live model should type.

## 4. The eval command reads the tenant UNGATED

The chicken and the egg: the command that produces the evidence has to read
the tenant file that lacks it. `load_tenant` gained `check_evals: bool =
True`, and `engine evals run` is the only caller that passes `False`. It is
not a general escape hatch and no environment variable turns the gate off:
an owner who wants the tier anyway runs the set.

## 5. The eval set owns its prompt, and the contract is what stays pinned

A case could have called the live site's prompt builder instead of carrying
its own copy. Two reasons it does not. The layering: `core/llm` is the seam
that `core/agents` sits on, and a harness reaching back up into the agents to
build a prompt inverts it. The measurement: an eval set is a fixed probe. Its
job is to compare models against each other on an unchanged task, so a
production prompt reworded next month should not silently move every score.

What the set does NOT copy is the contract: `job.json` names the site's own
pydantic model by import path (`core.agents.ap.extraction:ExtractionReply`),
so the reply shape, the `DecimalString` money rule, and every field name are
the real ones, and a test pins that the declared model IS the class the site
validates into. `prompt_source` in each `job.json` records where the copy came
from and when. **[NEEDS REVIEW]** prompt drift is the accepted cost: a site
whose prompt changes materially wants its eval set reviewed, and nothing
automated says so today.

## 6. The results file is a measurement record, not a benchmark

Schema: job, model id, adapter, tier, `seeded`, run timestamp, engine commit,
the provider model ids that actually answered, the totals, a summary line,
and every case with its checks (field, expected, actual, passed).
Deterministically ordered (cases by name, checks by field) so a re-run is a
readable diff. No cost and no latency: those belong to `llm_calls`, which
records production calls. `provider_models` earns its place because a tier
whose model id is the sentinel `default` says nothing about what answered;
the file names it.

**[NEEDS REVIEW]** that sentinel is the gate's blind spot. The seat's id
stays `default` while the model behind it changes underneath, so the gate
cannot notice. The results file records the provider id as evidence after the
fact, which is the honest half-measure available today.

## 7. The results file is not a run-key input

A run key digests what a JOB reads. The eval gate is config validation: it
runs at `load_tenant`, before any job, and re-scoring a model changes no job
output. Folding it into a key would re-execute intake every time somebody
re-ran an eval. `docs/run-keys.md` says so explicitly. What DOES ride the
keys, since row 7.10, is the resolved `(tier, adapter, model)`: repointing a
job re-extracts, which is the correct and separate behaviour.
