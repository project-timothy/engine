# Run keys: declare the inputs, let the audit catch the rest

Issue #153. Companion to invariant 3 in `CLAUDE.md` ("every ledger write
carries an idempotency key; every job is safe to re-run").

## The disease

A job's run key is a digest of its inputs; same key, same result, no work.
Five separate incidents in July and August were one disease: a key that
missed an input, so a changed input replayed the prior result.

| Incident | The missing input |
|---|---|
| 2026-07-20 (PR #117) | vendor registry: onboarding a vendor replayed the FLAGGED intake |
| 2026-08-13 (PR #121) | an unknown drop-tree folder: nothing else changed, the warn never fired |
| assessment #135 (PR #141) | the resolved month: September's default-month report replayed August |
| #133 (PR #137) | the code: a semantics fix replayed the old result until `MATCH_VERSION` rode the key |
| #136 (PR #143) | a landing change re-parked the same card (subject keys) |

Each fix patched one key by hand, and every other key stayed exactly as
forgetful as before.

## The builder

`core/engine/runkey.py` provides `RunKey`. A key function lists what the run
depends on, in the order a reader would want to see it:

```python
def _intake_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "intake")            # tenant, job, code version, shadow/live
    key.param("since")                     # a --param
    key.env(EXTRACTOR_ENV)                 # an environment override
    key.vendors(_vendors(ctx))             # the whole vendor registry
    key.config("ap")                       # a tenant-config section (or a dotted leaf)
    key.files(_candidates(ctx), digest=_md5)   # name + content, order-independent
    return key.digest()
```

Other parts: `value(label, obj)` for any JSON-able object (pydantic models,
paths, sets, Decimals are handled), `rows(label, rows)` for ledger rows,
`file(path)` for one file, `stamp()` for jobs that must never replay (close
preflight/packet), and `version="N"` on the constructor for a code change
that must re-run the same inputs (the `MATCH_VERSION` pattern).

Sentinels keep a key total when bytes are unreadable: a cloud-evicted
placeholder hashes as `cloud-only` (the 2026-07-09 EDEADLK lesson) and a
path that is not a file as `missing`; the digest replaces the sentinel the
moment content lands, so the run re-fires.

## The audit

Declaring is only half. The runner traces what a job actually reads while it
executes `key(ctx)` and `run(ctx)`:

- tenant-config leaf attributes, through a `TracedConfig` proxy
  (`ctx.tenant.expenses.category_accounts` records `expenses.category_accounts`);
- `ctx.params` lookups, through `TracedParams` (`param:month`);
- vendor-registry loads (`registry:vendors`, recorded inside
  `load_vendor_registry`).

Every input `run` touched must be declared by `key`, or read by `key` itself
(which puts it in the digest by construction). Coverage is by dotted prefix:
declaring `expenses` covers every leaf beneath it. `key.ignore(path,
reason=...)` declares a read the key deliberately omits and documents why at
the call site.

Under `ENGINE_KEY_AUDIT=strict` an undeclared input turns the run into an
error result (`job.exception: UndeclaredInputError ... read inputs its run
key does not declare: param:month, registry:vendors`). The root
`conftest.py` sets strict for the whole suite, so a job that grows a new
config read without folding it into its key fails its own existing evals,
with the missing inputs named. Production never sets the variable and is
never traced; `ENGINE_KEY_AUDIT_LOG=<file>` appends one line per offending
job when you want a migration checklist from one suite run.

## What the first audit pass found

Migrating every key (2026-08-26) surfaced gaps the hand-rolled keys had
missed, all of the replay shape:

- `ap/reconcile` never folded `qbo.reconcile_ignore_payees`, `--param
  ignore_payees`, or the vendor registry: adding an ignored payee replayed
  the prior settle run.
- `expenses/match` missed `match_window_days`, `default_channel`, the bank
  CSV format, `close.bank_account`, and the tenant timezone.
- `ap/verify-payment` missed the bank CSV format; `mail/fetch` missed
  `max_bytes` and the cc-route destination; `ap/intake` and `ap/apply`
  missed `filing_month_template`; the workbook, the demo, the expense
  report, and the statements missed `identity.legal_name` (the metadata
  they stamp into Office files).

## Deploy note

Every key's digest changed with the migration, so the first post-merge run
of each daily job executes once instead of replaying. All of them are
bookkeeping-neutral on re-execution by design: intake and apply dedup on
content, cards dedup on subject (#143), mail dedups on saved events, push
finds nothing unpushed, reconcile settles nothing new.

## A retry keeps the key

A cross-run retry (`docs/retries.md`, row 7.23) is the original run
resumed: the key builder does not run again, the attempt executes under the
original key with the original params, and the ok run row lands under that
key. The strict audit is skipped on a resumed attempt (there is no key
trail); the job passed it on its first attempt.

## What is deliberately NOT an input

The model eval results (`core/llm/eval_sets/<job>/results/<model_id>.json`,
row 7.13) do not ride any key. A run key digests what a JOB reads; the eval
gate is CONFIG VALIDATION, checked in `load_tenant` before any job starts,
and re-scoring a model changes no job's output. Folding it in would
re-execute intake every time somebody re-ran an eval set. What does ride the
keys, since row 7.10, is the resolved `(tier, adapter, model)`: repointing a
job at another tier re-extracts, which is the separate and correct rule.

## Rules for new jobs

1. Build the key with `RunKey`. Hand-rolled `hashlib` keys are the disease.
2. Declare every config section or leaf, every `--param`, and the registry
   the run reads. When in doubt, declare the section: a re-run on a
   cosmetic config edit costs nothing; a replay on a real one costs an
   incident.
3. Bump `version=` when the job's semantics change (a code fix must re-run
   the same inputs).
4. If the strict audit names an input you believe should not re-fire the
   job, `ignore()` it with the reason. The reason is the review artifact.
