# Agent: demo

## Role

The demo agent exists to prove the engine pipe end to end with no business
logic: CLI invocation, tenant config loading, a ledger write, idempotency, a
structured result object, and a git commit. It is the reference for what a
real agent's anatomy looks like (brief + jobs + schema + evals shipped in one
PR), not a production agent. It moves no money and sends nothing external, so
it never needs the approval queue.

## Jobs

- `ingest`: read a small JSON batch fixture, validate it against the input
  contract in `schema.py`, and record one `demo.item.ingested` event per item.
  The run's idempotency key is derived from the tenant slug plus the fixture
  bytes, so re-running with the same input is a verified no-op.
- `flaky`: the cross-run retry fixture (docs/retries.md). It declares a
  `RetryPolicy` (3 attempts, backoff 60 s then 300 s, retried on
  `transport_error` and `timeout`), fails `--param fail_times=N` times
  (default 2) with `--param cause=<label>` (default `transport_error`;
  `--param transient=no` marks the failure not transient), then succeeds.
  Every attempt records itself; the first also records one durable move
  before dying, so the attempt that succeeds finds the move on the record
  and skips it. Exists only so the retry path has a deterministic witness;
  no production job declares a policy in this row.

## Judgment guidance

None. Every step is deterministic. There is no LLM call in this agent and no
LLM call anywhere on a Phase 1 code path (the foundation is deterministic;
judgment steps arrive with the real agents in later phases).

## Escalation rules

None. A malformed fixture fails the input-contract validation and surfaces as
a job error; it is not silently swallowed.

## Input / output contract

Input: `schema.DemoBatch` (a `batch_id` and a list of `DemoItem`).
Output: a `JobOutput` with one event per item and a one-line summary. The
runner wraps that into the standard `RunResult`.
