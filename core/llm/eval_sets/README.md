# Eval sets per model job

Phase 7 rows 7.13 and 7.13b. The harness is `core/llm/evals.py`; the design
note is `docs/model-seam-design.md` ("The eval gate") and the decisions are
`docs/decisions/2026-09-16-eval-gated-tier-changes.md` (the gate) and
`docs/decisions/2026-09-16-a-case-states-the-part-under-test.md` (a reply
holding a list of objects).

One directory per model job:

```
<job>/
  job.json          the reply contract + the probe prompt
  cases/*.json      3 to 5 cases: a note, the document text, the expected fields
  documents/*.pdf   the file each case attaches
  results/*.json    one file per model scored, named by the model id
```

A job with a `cases/` directory is GATED: `load_tenant` refuses a tenant whose
`[llm.jobs]` points that job at a model with no green results file for the
tier's adapter. Adding a set is therefore how the gate is turned on for a job,
and the set has to arrive with results for every model the shipped tenants
already use (`fixture-model` for the demo tenant and every `engine init`
tenant, plus whatever the live tenants name).

## job.json

| key | meaning |
|---|---|
| `job` | the job type, the same string as the directory name |
| `output_model` | `module:attr` of the pydantic model the SITE validates replies into. Never a copy: the contract under test is the real one |
| `decimal_fields` | fields compared as numbers rather than text, so `10.0` and `10.00` agree (a leading `$` and thousands commas are stripped) |
| `system` | the system turn: the job's brief |
| `user` | the ask. `{document_text}` is replaced with the case's text and `{page_count}` with the pages of the case's document; a job whose live site does not inline the text layer (the receipt inbox reads a photograph, the scan grouper reads the pages) leaves the text token out |
| `prompt_source` | where the prompt was copied from and when. An eval set is a fixed probe, so it carries its own copy on purpose |

## A case

```json
{
  "name": "invoice_plain",
  "note": "why this case exists and which rule it tests",
  "document": "invoice_plain.pdf",
  "text": "the document's text, line for line",
  "expect": {"doc_type": "invoice", "amount": "450.00"}
}
```

`expect` is a subset: only the fields listed are checked, exactly, with `None`
and `""` reading the same. Keep expectations unambiguous. A field two careful
readers would answer differently (the "total due" on a quotation) makes a bad
case: assert the rule the case is about and leave the rest alone.

The subset rule reads all the way down, which is what a reply holding a list
of objects needs (`scan_group`: the grouping is under test, the vendor and
amount that name the child file are not). A dict expectation checks the keys
it names; a list expectation checks position by position with the length under
test:

```json
"expect": {"groups": [{"pages": [1, 2]}, {"pages": [3]}]}
```

That stays a VALID partial reply, which is what lets the seeded fixture run
answer with it and the contract fill in the rest. The results file still
records the whole reply the model gave, so a reader sees everything that was
not asserted.

Every document is `conftest.minimal_pdf(case["text"])`, and a test pins that,
so the file and the inlined text can never drift apart. One form feed (`\f`)
in the case text is one page break in the document, so a multi-page case is
still one readable string. Cases carry synthetic businesses and synthetic
amounts only, the same shapes the agent evals already use.

A case states a right ANSWER, and there is no way to state a refusal. What the
engine does with a bad reply belongs to the code that owns the rule (the split
pass holds a scan whose groups do not cover every page exactly once), so it is
tested where it lives and the case note names the failure mode it guards.

## Scoring a model

```
uv run engine evals list
uv run engine evals run <job> --tenant <slug> --tier <name>   # a real model
uv run engine evals run <job> --model <id>                    # seeded fixture run
```

The second form answers every case from the case's own expectation, so it
scores the HARNESS: the file it writes is marked `seeded` and opens the gate
for a fixture tier only. Commit the results file with the change that needed
it.

`engine_commit` in a results file is the HEAD the harness ran from, which is
always the commit BEFORE the one that carries the file: a run cannot record
the commit it is about to be committed into. Read it as "the tree that
scored this", not as "the commit this file belongs to".
