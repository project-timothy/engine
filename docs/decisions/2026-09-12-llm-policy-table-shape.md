# The `[llm]` policy table shape, the `llm_calls` row, and the fallback rule
Date: 2026-09-12
Type: Refines 2026-09-11

Phase 7 row 7.9 (issue #218) implements the policy table the model-seam
design documented. The plan and `docs/model-seam-design.md` fixed the
three table names, the `deterministic` tier meaning, the `llm_calls`
columns the row must carry, and the budget refusal as an anomaly. These
are the choices the row made that neither specified.

**A tier names its adapter explicitly.** The design sketch derived the
adapter from a `<provider>/<id>` prefix on the model string. The tier is
instead `{ adapter, model, base_url, api_key_env, pricing, fallback }`: the
adapter name is a field validated at config load against `LLM_ADAPTERS`,
and the model id is passed to the provider untouched, so a provider that
uses slashes in its own ids (a local gateway, OpenRouter) never collides
with the seam's routing. `pricing` (USD per million tokens in and out,
Decimals) is required on every tier so `usd` is never unknown on a policy
call; `api_key_env` is a variable NAME, never a value.

**`claude_agent_sdk` is a describe-only adapter name.** The first tenant's table
must state what runs today (the Claude Agent SDK under the owner's seat,
model unset) before rows 7.10 to 7.12 move the sites. The name is legal
in `[llm.tiers]` so the file loads; `build_adapter` refuses it with a
typed `LlmPolicyError`, so nothing can call through it by accident. Row
7.15 decides whether an SDK `complete()` adapter exists.

**The fallback rule, two halves.** Job to tier: `[llm.jobs]` names the
tier; an unlisted job takes `[llm.jobs].default` when present and is
otherwise refused (`UnresolvedJobError` naming the job). A job marked
`deterministic` raises `DeterministicJobError` naming the job before any
adapter is built. Tier to tier: a tier's `fallback` lists OTHER tier names
(not model ids, so the fallback carries its own adapter and price), walked
in order on a transient transport failure only; a validation failure or a
non-transient transport failure (refusal, no key) stops at the first tier.
Every attempt writes its own `llm_calls` row.

**`llm_calls` carries three columns the plan did not list, and no
idempotency key.** `tier` (the policy name that resolved, so a tier change
is visible in telemetry without a join), `status` (`ok`,
`validation_failed`, `transport_failed`, `budget_refused`), and `detail`
(the last validation error, the transport cause, or the refusal text). A
failed call is still a call and gets a row; the gateway raises without a
`CallRecord`, so a failed row carries zero tokens and zero usd, a known
undercount the gateway's next revision can close. A budget refusal is a
row with `status = budget_refused` and no adapter touched. A telemetry
row is an observation, not a business write that must be safe to replay,
so the table has no `idempotency_key`; the run key on the row is what ties
it to a run.

**The cap refuses at spent >= cap.** The month is the UTC calendar month
of `created_at`, summed per tenant across every run, live or shadow. A
call is refused once the recorded spend has reached the cap, so the cap is
a ceiling the tenant never crosses by one more call. `monthly_usd` absent
is no cap.

**Totals ride the run result.** `RunResult.llm` is `{calls, tokens_in,
tokens_out, usd}` summed from the run key's rows at the moment the run
lands (ok or FAILED); `calls` counts gateway calls, refusals excluded. The
runner reads the rows back rather than trusting an in-memory tally, so a
job that died after a call still reports the call. Each refusal row
becomes an `llm.budget` anomaly on the result, whether or not the job
caught `BudgetExceeded`.

Refines `2026-09-11-model-gateway-interface.md`: `complete()` is unchanged;
`complete_for()` wraps it. Supersedes the "documented here, not
implemented" status of the policy section in `docs/model-seam-design.md`.
