# Retry policy on the handler; attempts as `engine.retry` job records
Date: 2026-09-12
Type: Two-way door

Phase 7 row 7.23 (issue #232) adds bounded cross-run retries. The plan
named the row (`job_records.retry_policy`, `engine jobs resume`) and left
two shapes open: where a job declares its policy, and whether the attempts
live on `job_records` or a sibling table. Both are decided here.

**The policy lives on `JobHandler`.** `JobHandler(key, run, retry=None)`;
`RetryPolicy` is a pydantic model of `max_attempts` (counting the first),
`backoff_seconds` (a ladder whose last rung repeats), and `retry_on` (cause
labels). The smallest primitive consistent with `JOBS: dict[str,
JobHandler]`: one optional field, no decorator, no registry, default none
so every existing job is untouched. The policy is code, not tenant config,
because whether a failure is worth redialing is a property of the job's
transport, not of the business running it; a tenant that wants a different
budget changes it on a coding day like any other job semantics.

**Attempts are `job_records` rows, not a sibling table.** Migration 8 adds
five columns (`retry_policy`, `attempt`, `next_attempt_at`,
`parent_record_id`, `retry_state`) and one index; each attempt of a
policy-holding job is one record of type `engine.retry`, written in the
same transaction as the run row it belongs to. Chosen over `job_retries`
because PRD v2 already names `job_records` as the durable job ledger the
status page (row 7.24) renders from, because a plain record and a retry
record share every other column (run key, run id, tenant, agent, job,
payload, created_at), and because the migration 6 precedent of "the
record is durable the moment it is written and the runner links it to the
run" is exactly the property a retry needs. A plain record reads NULL / 0
/ '' in the new columns and means what it always did.

**A retry is the same run resumed under the original key.** The key
builder does not run on a resumed attempt; the record carries the original
params; the ok run row lands under the original key. This is what makes a
retry after a partial write safe (job-time records and events are keyed
under the run key, so the succeeding attempt finds what attempt 1 wrote).
A hand re-run while a retry is pending joins the chain as the next attempt
(compare-and-set on the record) so one key never has two chains.

**The failed-run trace is untouched, to the key.** Every failed attempt
still records the FAILED row under `<key>:failed:<stamp>`, the
`engine.run_failed` event, and the FAILED commit, and the event payload
keeps exactly the keys it had. The draft of this row put the `cause` on that
event too; it came out, because a job with no policy must leave a trace
byte for byte identical to today's and the cause is not this row's business
to add to every failed run in the engine. The cause lives on the retry
record, where the retry decision that read it lives. The retry record is
added beside the trace, never in place of it.

**Cause, not exception type.** The runner reads `cause` and `transient`
off the raised exception, the taxonomy `ExtractionError` and
`GatewayTransportError` already carry; a bare exception is `exception`, a
reported error is `reported` and never transient, and a shadow run never
retries. Only a listed cause that is not declared non-transient earns a
retry; the budget spent, the record reads `exhausted` with the count and
is never picked again.

**Nothing is scheduled.** This row ships `engine jobs resume` and its
`--now`; the 15-minute cadence is row 7.21's crontab. No production job
gains a policy; `mail/fetch` and the QBO transport paths are the named
first candidates for a coding day.

Two-way: no existing row changes meaning, no job changes behavior, and
the columns can sit unused. The migration itself is flagged in the PR as
the ledger-schema item CLAUDE.md requires.
