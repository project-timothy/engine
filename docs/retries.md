# Cross-run retries: `RetryPolicy` and `engine jobs resume`

Phase 7 row 7.23, issue #232. Decision:
`docs/decisions/2026-09-12-retry-policy-on-the-handler.md`. Companion to
`docs/run-keys.md` (a retry keeps the original key) and to the failed-run
trace of 2026-09-03 (a retry never weakens it).

## Two kinds of retry, one word

The engine already retries INSIDE a run: `RetryingExtractor` redials a
transient extraction failure up to three times inside one intake pass, the
gateway walks a tier's `fallback` chain on a transient transport failure,
and the policy layer gives a reply that fails validation one more turn.
Those are in-process: the job is still running, nothing is on the ledger
yet, and the redial is over in seconds.

This page is the other kind. A run has FAILED and been recorded as such
(the FAILED run row, the `engine.run_failed` event, the FAILED commit). The
process is gone. Minutes later a scheduler resumes the same run. That is a
cross-run retry, and it is the only thing `RetryPolicy` governs.

## The policy shape

Declared on the handler, in the agent's `JOBS` table; default none, so a job
without one behaves exactly as before:

```python
JOBS = {
    "fetch": JobHandler(
        key=_fetch_key,
        run=_fetch_run,
        retry=RetryPolicy(
            max_attempts=3,                      # counts the first attempt
            backoff_seconds=[300, 1800],         # before attempt 2, 3, ... (last rung repeats)
            retry_on=["transport_error", "timeout"],
        ),
    ),
}
```

`retry_on` names failure causes from the taxonomy the code already raises
with: an exception's `cause` attribute (`ExtractionError`,
`GatewayTransportError`, the demo's `DemoTransportError`). A bare exception
is cause `exception`; a reported error (`JobOutput(status="error")`) is
cause `reported`. A failure earns a retry only when its cause is listed AND
it does not declare itself `transient=False` (a provider's 4xx is
`transport_error` with `transient=False`: same label, no redial). Shadow
runs never retry.

## What lands on the ledger

Every attempt of a job with a policy is one `job_records` row of type
`engine.retry` (migration 8 added the columns: `retry_policy` json,
`attempt`, `next_attempt_at`, `parent_record_id`, `retry_state`). The
record is written in the same transaction as the run row it belongs to and
carries the original `--param` set, so the resume needs nothing else.

A job that fails twice and succeeds the third time leaves:

| record | attempt | parent | state | owned by |
|---|---|---|---|---|
| 1 | 1 | none | `resumed` | FAILED run row `<key>:failed:<stamp>` |
| 2 | 2 | 1 | `resumed` | FAILED run row `<key>:failed:<stamp>` |
| 3 | 3 | 2 | `succeeded` | the ONE ok run row under `<key>` |

States: `scheduled` (the next attempt is due at `next_attempt_at`),
`resumed` (that attempt started; its own record follows), `succeeded` and
`exhausted` (terminal; never picked again). A record left `resumed` with no
child means the process died mid-attempt; it is visible, and it does not
loop.

Past the budget the last record reads `exhausted` with the attempt count,
the run stays FAILED, and the result carries `engine.retry.exhausted`
("attempt 3 of 3 failed (transport_error): retries exhausted at 3"). A
cause outside `retry_on` writes no record at all; the result carries
`engine.retry.not_retryable` and the FAILED trace stands as it always did.

## The original key, resumed

A retry is the same run resumed, never a new input set. The key builder
does not run again; the attempt executes under the run key of the original,
with the original params, and lands the ok run row under that key. That is
what makes a retry after a partial write safe: the job-time records
(`ctx.record_now`) and the events are namespaced under the run key, so the
attempt that succeeds finds what attempt 1 already wrote and skips it
(`demo/flaky` proves this: one move record, still owned by the FAILED run
that made it). The strict run-key audit does not run on a resumed attempt
(there is no key trail to audit against); the job passed it on attempt 1.

A hand re-run (`engine run ...`) while a retry is pending under the same
key joins the chain as the next attempt. There is never a second chain
under one key.

`RunResult.llm` on a resumed attempt sums every `llm_calls` row under the
run key, earlier attempts included: the same run, the same bill.

## `engine jobs resume <tenant>`

Executes the retries whose `next_attempt_at` has passed, oldest first, one
at a time under the run lock (a held lock is a structured refusal and the
record stays scheduled). Nothing due prints one line and exits 0:

```
$ engine jobs resume demo
no retries due for demo
```

Otherwise one block per attempt, exit 1 when any attempt ended in error:

```
$ engine jobs resume demo --now
demo/flaky @ demo: ok (attempt 3 of 3 of run demo.flaky.3f2a... (resumed from retry record #2))
  flaky: succeeded on attempt 3 for Demo Co; the move was already on the record
```

`--now` is the owner's tool: every scheduled retry runs now, due or not.
`--json` emits the results.

## The cadence

Row 7.21 wires the scheduler: one crontab line in the container,

```
*/15 * * * *  engine jobs resume <tenant>
```

This row ships the command and nothing else. No host has the cadence yet;
the first tenant's Mac gets no launchd job from this row. Until 7.21 lands, a due
retry waits for the owner's `--now`.

## Who gets a policy

Nobody yet. Every production job runs exactly as before; `demo/flaky` is
the only holder and exists for the tests. The first candidates, for a
coding day with the owner present, are the jobs whose failures are the pipe
rather than the document: `mail/fetch` (Graph transport) and the QBO
transport paths (`ap/push`, `ap/reconcile`). A validation verdict
(`bad_reply`, `oversize`, a precondition) never earns one.
