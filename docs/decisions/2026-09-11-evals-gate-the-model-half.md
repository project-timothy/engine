# Evals gate the model half
Date: 2026-09-11
Type: Refines 2026-06-10

The code half already has the rule
that an incident gets an eval before its fix (a 2026-06-10 decision,
"Incidents-to-evals as learning loop v1"). v2 extends the same gate to the
model half:

- Every job type that calls a model owns a labeled eval set under
  `evals/<job>/`.
- Every skill (a prose contract a model executes) owns a fixture-driven
  harness: "given this audit report, the note must contain these sections
  and never these words." Phase 7 row 7.14 ships the first harnesses.
- A model or tier change in `tenant.toml` is refused at startup unless the
  eval results for that model are on file.

A prompt edit becomes a gated change like any other. Two rules from the
decision table ride with it: a model call site ships with a fixture
implementation so the suite runs with no network and no key, and money
fields are `Decimal` strings re-parsed in code so a model never writes a
number that lands in a ledger.

Why now: risk 6 in the PRD (model-half drift) names the gap, a prompt edit
today has no failing test, and the mitigation is skill evals in phase 7
before any new headless lane. The phase 7 exit criterion says the same:
every model site has an eval set and a fixture, and skill edits fail CI
when they break the harness.
