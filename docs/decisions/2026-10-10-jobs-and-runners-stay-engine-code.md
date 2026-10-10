# Jobs and agent runners stay engine code; the contracts package holds the adapter seams
Date: 2026-10-10
Type: Two-way door

Issue #344 (extraction gate 3 remainder).

## Decision

`core/contracts/` (Apache-2.0) holds two seams: the mailbox (`mail.py`, #329) and
the model provider (`llm.py`: `Adapter`, the prompt and reply shapes it names, and
the transport error it raises). `core/llm/gateway.py` re-exports every moved name,
so existing imports and `isinstance` checks see the same classes.

The job contracts (`JobContext`, `JobHandler`, `JobOutput` and friends) and the
agent-lane `Runner` stay in the engine, under the AGPL.

## Why

An adapter talks to one outside system and needs nothing from the engine but the
shapes it receives and returns. Those move cleanly.

A job and a runner are different. `JobContext` hands a job the tenant's whole
configuration, the ledger, and the write guard, and jobs read deep into all three
(`ctx.tenant.ap.protected_paths`, `ctx.tenant.close.bank_account`, ...). The
"small Protocols" option in #344 (`TenantLike`, `LedgerLike`, `GuardLike`) would
have to restate the entire `TenantConfig` to type a real job, which is a copy of
the engine's schema under a second license, not a small seam. `Runner.run` takes a
`Session` that carries the tool gate, the transcript, and the budget accounting:
the same problem.

So the honest statement is the one #344 offered as option 2: adapters are
pluggable under Apache-2.0; jobs and runners are engine code.

## Revisit when

An outside author wants to ship a job privately. Then the right move is probably a
narrower job API (a read-only tenant view plus the record and event calls), designed
for that author, rather than Protocols over today's classes.
