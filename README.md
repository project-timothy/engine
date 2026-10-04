# timothy-engine

A business-agnostic back-office agent engine. Every back-office job is an
idempotent CLI invocation against a git-backed ledger, every agent is a brief
plus deterministic code versioned together, every tenant is pure configuration,
and every past incident is a regression test.

Phase 1 is the foundation: ledger core, tenant config loader, run framework,
eval harness, CI, and seed evals. No AP/AR/close business logic yet. See
`CLAUDE.md` for the engine invariants, `docs/boundary-rules.md` for what is
code and what is a model, and `docs/archetypes.md` for the business shapes.

## Setup (clean machine, under 10 steps)

1. Install [uv](https://docs.astral.sh/uv/) (`brew install uv` on macOS).
2. Ensure git is installed and a Python 3.12 interpreter is available
   (`uv python install 3.12` if not).
3. Clone this repo and `cd` into it.
4. `uv sync` — creates the virtual env and installs pinned dependencies from `uv.lock`.
   Add `--extra claude` on a host that drives model jobs through Claude Code (the
   Claude Agent SDK); a host on API keys alone does not need it, and the suite passes without it.
5. Run a job: `uv run engine run demo demo ingest`.
6. Run it again: `uv run engine run demo demo ingest` — reports `noop` (idempotent).
7. Run the test suite: `uv run pytest`.
8. Lint: `uv run ruff check .` and check the tenant boundary: `uv run python -m core.evals.bleedthrough_lint`.

That is the whole loop. No secrets, no network, no cloud accounts are needed to
run Phase 1.

## What the demo proves

`engine run <tenant> demo ingest` exercises the entire pipe: CLI dispatch,
tenant config loading, a ledger write, idempotency, a structured result object,
and a git commit. One tenant ships: `demo`, a synthetic tenant that proves the engine is
generic; `engine init` renders a real one (`docs/install.md`). The ledger for each run lands under
`./.ledger/<tenant>/` by default (override with `--ledger-dir` or
`$ENGINE_LEDGER_ROOT`); it is its own git repo and is gitignored from this code
repo.

## Commands

| Command | What it does |
|---------|--------------|
| `uv run engine run <tenant> <agent> <job> [--shadow] [--json]` | Run a job against a tenant's ledger |
| `uv run engine agents` | List discoverable agents and their jobs |
| `uv run pytest` | Unit tests (`tests/`) + evals (`core/**/evals/`) |
| `uv run ruff check .` / `uv run ruff format --check .` | Lint / format check |
| `uv run python -m core.evals.bleedthrough_lint` | Fail if any tenant token leaked into `core/` |

## Repository layout

```
core/engine/    runner, CLI, tenant config, RunResult contract
core/ledger/    SQLite schema + migrations, JSONL event log, git-commit wrapper
core/agents/    one directory per agent (brief.md + jobs.py + schema.py + evals/)
core/evals/     eval harness, seed incident regressions, bleed-through lint
core/learning/  incident-to-eval converter
tenants/<slug>/ tenant.toml and registries (all tenant-specific values)
docs/           archetypes.md, boundary-rules.md, incident-template.md, decisions/
```
