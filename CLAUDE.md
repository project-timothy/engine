# CLAUDE.md — timothy-engine

Read this before writing code. This file
carries the engine invariants so every session loads them, plus the operating
rules a fresh session needs.

## What this is

A business-agnostic back-office agent engine. Every back-office job is an
idempotent CLI invocation against a git-backed ledger; every agent is a brief
plus deterministic code versioned together; every tenant is pure configuration;
every past incident is a regression test.

## Engine invariants — non-negotiable

1. **The ledger is truth.** Cloud drives are delivery views, hash-verified on every push.
2. **No LLM output ever writes money values or ledger mutations directly.** Code computes; the LLM classifies, drafts, and flags.
3. **Every ledger write carries an idempotency key.** Every job is safe to re-run.
4. **Three-way payment verification** (AP ledger, cleared bank, bill-pay queue) is engine code, not agent judgment. Written in the ~$38K near-miss of 2026-05-21.
5. **Nothing under `core/` references a tenant.** CI enforces (bleed-through lint).
6. **Agent brief and agent code ship in the same PR.**
7. **Every external send or money movement passes through the approval queue.** No exceptions, including "hurry."
8. **Incidents get an eval before the fix merges.**
9. **Pinned dependencies; CI fails on unpinned additions.**
10. **Office-format deliverables** set author/company metadata to the tenant's legal name, use python-docx/openpyxl/python-pptx, and follow the xlsx hardcoded-values recipe.

## Operating rules

- **No LLM calls on Phase 1 code paths.** The foundation is deterministic.
- **TDD is standing policy.** Write the failing test or eval before the implementation, every module, every branch. PRs showing implementation without tests do not pass the gate.
- **Two trees:** unit tests under `tests/` prove the code; evals under `core/**/evals/` test incident regressions. Both run in CI.
- **Pin everything.** uv lockfile is committed. CI fails on an unpinned dependency.
- **Who merges (2026-09-27):** an agent merges only a PR opened by the host's own lanes, and only on green gates stated with their numbers. A PR from any other author, a contributor or a fork, waits for a maintainer's review and merge; no agent merges it, whatever its checks say. A host deploys only from the repository it trusts (its private main), never from a branch or repository an outside author can write to.
- **Commit hygiene:** feature branch per work item, conventional commit messages, PRs into the active phase branch. Never commit directly to `main`. Verify merged commits are ancestors of the target branch before calling work landed (2026-06-08 stacked-PR lesson).
- **One-way doors stop and flag.** Ledger schema changes, new external dependencies, anything touching money movement: stop and flag in the PR rather than deciding.
- **Secrets never enter the repo.** Tenant config names a secret's environment variable; resolution is at runtime.

## Layout

```
core/engine/    runner, CLI, tenant config, RunResult contract, RunKey builder + input audit (docs/run-keys.md)
core/ledger/    SQLite schema + migrations, JSONL event log, git-commit wrapper, idempotency
core/agents/    one dir per agent: brief.md + jobs.py + schema.py + evals/
core/evals/     harness, seed incident regressions, bleed-through lint + token list
core/learning/  incident-to-eval converter
auditor/        the independent auditor (docs/auditor-design.md): imports NOTHING from core/
                (CI-enforced by auditor.evals.independence_lint); own CLI, store (.auditor/),
                lenses, and nightly checklist report
tenants/<slug>/ tenant.toml (+ vendors.toml, rules/, secrets.ref). All tenant-specific values live here.
                A real tenant lives in its OWN repository since 2026-09-22 and is found through
                ENGINE_TENANTS_ROOT (engine and auditor alike); only demo/ and _templates/ ship here.
docs/           archetypes.md, boundary-rules.md, lessons.md, auditor-design.md, incident-template.md,
                decisions/ (one file per decision)
```

## Run it

```
uv sync --extra claude                         # the extra = the Claude Agent SDK; omit on an API-keys-only host
uv run engine run <tenant> <agent> <job> [--shadow]
uv run engine doctor <tenant>                  # what this host still needs (row 7.21)
uv run engine schedule <tenant>                # render this host's crontab from [host.schedule]
uv run engine status-page <tenant>             # the read-only status page (row 7.24)
uv run auditor run <tenant> [--local-only]     # the independent nightly audit
uv run pytest                                  # unit tests + evals
uv run ruff check . && uv run ruff format --check .
uv run python -m core.evals.bleedthrough_lint  # tenant-boundary lint
uv run python -m auditor.evals.independence_lint  # auditor-imports-nothing-from-core lint
```

## Adding an agent

Create `core/agents/<name>/` with `brief.md`, `schema.py` (pydantic input/output
contract), `jobs.py` exposing `JOBS: dict[str, JobHandler]`, and an `evals/`
directory. Brief and code ship in the same PR (invariant 6). See `core/agents/demo/`.
A job that MOVES a file outside the ledger records it first: `ctx.record_now(key, record_type,
payload)` is durable the moment it is called (the `job_records` table, 2026-09-10, honesty audit
03-F9); the runner links it to the run, and the next run heals a record whose event never landed.
Build every job's key with `RunKey` (`docs/run-keys.md`): declare each config
section, `--param`, file set, and registry the run reads. The test suite runs
under the strict input audit, so an undeclared read fails the job's own evals.
A job whose failures are the pipe, not the document, may declare
`JobHandler(..., retry=RetryPolicy(...))` (`docs/retries.md`): bounded cross-run
retries that `engine jobs resume` executes; default none.
