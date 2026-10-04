# The advisory drafter's vendored seam: prose out, one call, no telemetry row
Date: 2026-09-15
Type: Refines 2026-09-11

Phase 7 row 7.12 (issue #221) moves `auditor/advisory/draft.py` off its
hard-coded Claude Agent SDK call and onto the tenant's `[llm]` policy. The
auditor imports nothing from `core/` (`auditor.evals.independence_lint`), so
`docs/model-seam-design.md` already said this row would vendor a minimal copy
of the seam rather than import `core.llm`. These are the choices the row made
that neither the plan row nor the seam note specified.

**The vendored copy is `auditor/advisory/llm_client.py`, and it returns
PROSE.** The engine's `complete()` is built around a pydantic output model:
validation is the boundary, money is a `DecimalString`, and a reply that does
not validate never reaches a caller. The advisory contract has nothing for
that machinery to hold. The drafter's entire output is one string that lands
in one report section (auditor design, invariant 2's division of labor), so
there is no output schema, no JSON instruction in the prompt, and no
`DecimalString`. The vendored client keeps the shape that does carry over:
a tier resolved from `[llm.jobs]`, a prompt bundle, one adapter call, the
key read from the environment variable the tier NAMES. `tests/unit/
test_auditor_advisory_gateway.py` is the parity tripwire: adapter names,
the reserved `deterministic` word, the `default` job key, the Anthropic
endpoint and API version, and the no-auth placeholder must agree with the
engine's, and the vendored `Tier` may not carry a field the engine's
`ResolvedModel` does not.

**One call, no retry, no tier fallback walk.** `complete_for()` retries a
validation failure once and walks the tier's `fallback` list on a transient
transport failure. The drafter does neither, because it already has a better
second answer than a second model: `fallback_counsel` renders the same facts
deterministically. A redial buys a nicer voice at the cost of minutes on the
02:00 run and a second bill.

**A failure names itself in the report.** Any failure (no key, transport,
refusal, empty reply, a policy error, a missing SDK) degrades to
`fallback_counsel` AND the Advisory section prints one line saying the
counsel is the deterministic fallback voice with a short reason label.
`--local-only` gets the same line with reason `local-only`. The label comes
from a closed vocabulary (`failure_label`), never from an exception's text,
so an endpoint, a key variable name, or a provider's own words can never
reach the report. This extends the 2026-09-03 honesty rule ("the report
names every lens that did not run") to the voice itself.

**No `llm_calls` row.** The engine writes one telemetry row per gateway call.
The auditor cannot: the `llm_calls` table lives in the engine's ledger, which
the auditor opens READ-ONLY by design, and the auditor's own store has no
calls table. Inventing one would give the checker a write path it has never
had, for telemetry. So the advisory call is unmetered, and the tenant's
`[llm.budget]` cap does not bind it. At the first tenant the tier is the flat-rate
seat, so the unmetered call costs zero; the day a tenant points
`draft_advisory` at a metered tier, a nightly prose call of a few hundred
tokens is the exposure. Closing it means either a small table in the
auditor's own store or a one-way write, and that is an owner call, not this
row's.

**A tenant with no `[llm]` tables keeps the seat path.** `resolve({}, ...)`
returns the `claude_agent_sdk` tier rather than raising, and the seat client
does not pass the tier's model id to the SDK. Both keep an unconfigured
tenant byte-identical to the drafter before this row: the same one prompt
string, the binary on its own default model, one turn, zero tools. Naming
the tables governs; naming nothing changes nothing.

Refines `2026-09-11-model-gateway-interface.md` and
`2026-09-12-llm-policy-table-shape.md`: neither changes. This is the auditor
side of the same policy table, read by the auditor's own TOML reader.
