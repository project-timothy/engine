# timothy-engine

A business-agnostic back-office agent engine. Every back-office job is an
idempotent CLI invocation against a git-backed ledger, every agent is a brief
plus deterministic code versioned together, every tenant is pure configuration,
and every past incident is a regression test.

It ships eleven agents and their jobs: accounts payable (`ap`), accounts
receivable remittance (`ar`), month-end `close`, `expenses`, `timesheets`,
`mail`, `deadlines`, `projects`, a weekly `brief`, approval `routing`, and
`demo`; run
`uv run engine agents` for the full list. Beside the engine sits an
independent `auditor` that recomputes truth from ground sources every night
and imports nothing from the engine.

Read these first, in order:

1. `CLAUDE.md`: the engine invariants and the operating rules.
2. `docs/boundary-rules.md`: what is code and what is a model.
3. `docs/archetypes.md`: the business shapes a tenant can take.
4. `docs/install.md`: running it for a real business, in one container.
5. `CONTRIBUTING.md`: licensing, the gates, and who merges.

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
7. Give the demo its folders: `uv run engine doctor demo --create-folders` makes the
   `demo-data/` tree its `tenant.toml` names (the container does this at first boot).
   Now `uv run engine run demo ap intake` runs, with nothing to intake until you drop
   a PDF in `demo-data/inbox/`. Run `uv run engine doctor demo` again: it still
   names one item missing, the `ledger remote`, which only a host that pushes
   its ledger every night needs (`docs/install.md` step 5). The demo needs
   nothing else, and every `skip` line is a lane the demo never connects.
8. Run the test suite: `uv run pytest`.
9. Lint: `uv run ruff check .` and check the tenant boundary: `uv run python -m core.evals.bleedthrough_lint`.

That is the whole loop. No secrets, no network, no cloud accounts are needed to
run the demo.

## What the demo proves

`engine run <tenant> demo ingest` exercises the entire pipe: CLI dispatch,
tenant config loading, a ledger write, idempotency, a structured result object,
and a git commit. One tenant ships: `demo`, a synthetic tenant that proves the engine is
generic; `engine init` renders a real one (`docs/install.md`). The ledger for each run lands under
`./.ledger/<tenant>/` by default (override with `--ledger-dir` or
`$ENGINE_LEDGER_ROOT`); it is its own git repo and is gitignored from this code
repo.

## Commands

Every engine command takes `--help`. The owner-facing ones need a tenant.

| Command | What it does |
|---------|--------------|
| `uv run engine run <tenant> <agent> <job> [--shadow] [--json]` | Run a job against a tenant's ledger |
| `uv run engine agents` | List discoverable agents and their jobs |
| `uv run engine init <slug> [--archetype A\|B\|C] [--shape SHAPE]` | Create a tenant from an archetype template, with its kit (`docs/tenant-kit-design.md`) |
| `uv run engine onboard <slug> [--answer ID=VALUE] [--apply]` | The onboarding conversation as JSON for an agent: next question, record answers, then create the tenant and run doctor |
| `uv run engine voice-check <tenant> <file> [--register NAME]` | Check a draft against the tenant's `kit/voice.toml`: spelling, banned words, glossary, register |
| `uv run engine doctor <tenant>` | Report what this host is missing: secrets, folders, the ledger and its remote, the scheduler |
| `uv run engine schedule <tenant>` | Render this host's crontab from `[host.schedule]` |
| `uv run engine queue ...` | Review the approval queue |
| `uv run engine status <tenant> ...` | Owner write-back: mark an invoice scheduled or paid |
| `uv run engine status-page <tenant>` | Render the read-only status page |
| `uv run engine close-preflight <tenant>` | Month-end close checklist; exits 0 OK, 1 WARN, 2 BLOCK |
| `uv run engine sweep <tenant>` | Read-only: invoice-like files the engine has not booked |
| `uv run engine identify ...` | Record an unprocessed file as an invoice (manual entry) |
| `uv run engine dismiss ...` | Mark an unprocessed file as not an invoice |
| `uv run engine shadow-diff ...` | Read-only parity report: engine shadow rows vs a legacy ledger |
| `uv run engine jobs ...` | The job ledger: bounded retries (`engine jobs resume`) |
| `uv run engine runner ...` | Agentic sessions: `engine runner run <tenant> <skill path>` |
| `uv run engine mail ...` | Owner acts on the tenant mailbox connection |
| `uv run engine mcp <tenant> [--as <person>]` | Serve the read-only tools over MCP on stdio, for a chat client; `--as` answers as one person under authority.toml |
| `uv run engine mcp <tenant> --http --resource <url> --tokens <file>` | Serve the same tools on one HTTP endpoint for a hosted box, each request answered as the person its bearer token signs in |
| `uv run engine door-token <tenant> <person> --tokens <file>` | Make an invitation token for one person; printed once, the file keeps only its hash |
| `uv run engine mcp <tenant> --http --resource <url> --authorization-server <issuer> --introspect <url> --introspect-secret <file>` | The same endpoint, signed in through the box's sign-in service ([doorkeeper](https://github.com/project-timothy/doorkeeper)), which the engine asks who each token is (RFC 7662) |
| `uv run engine mcp --onboarding` | Serve the onboarding conversation over MCP, before any tenant exists; apply needs the person's yes |
| `uv run engine capabilities [--format json]` | What Tim can do today, built from the code: the tools, the setup questions, each lane in one plain sentence, and what is not built yet |
| `uv run engine evals ...` | Score a model on a job and write the results file the tenant config gate reads |
| `uv run auditor run <tenant> [--local-only]` | The independent nightly audit (`auditor --help` for the rest) |
| `uv run pytest` | Unit tests (`tests/`) + evals (`core/**/evals/`) |
| `uv run ruff check .` / `uv run ruff format --check .` | Lint / format check |
| `uv run python -m core.evals.bleedthrough_lint` | Fail if any tenant token leaked into `core/` |
| `uv run python -m auditor.evals.independence_lint` | Fail if anything under `auditor/` imports the engine |

## Repository layout

```
core/engine/    runner, CLI, tenant config, RunResult contract, RunKey builder, doctor, init
core/ledger/    SQLite schema + migrations, JSONL event log, git-commit wrapper
core/agents/    one directory per agent (brief.md + jobs.py + schema.py + evals/)
core/contracts/ the plug shapes adapters are built against; Apache-2.0, its own LICENSE
core/adapters/  the outside systems: bank files, mail and calendar, the accounting API
core/llm/       the model seam: gateway, policy, agentic runner, eval sets
core/tools/     the read-only tool catalog served over MCP
core/evals/     eval harness, seed incident regressions, bleed-through lint
core/learning/  incident-to-eval converter
auditor/        the independent nightly auditor: own CLI, store, and lenses; imports nothing from core/
evals/          the red tree: one failing eval per merged proposal, outside pytest's testpaths
skills/         skills the agentic runner executes (the morning audit triage)
host/           the container's entrypoint, crontab template, and secrets loader
scripts/        scheduled wrappers, the build-lane helper, and one-time accounting-API setup
tenants/        demo/ and _templates/ only; a real tenant lives in its own repository
tests/          unit tests; tests/skills/ runs the skill contracts with fixtures
docs/           design notes, install.md, lessons.md, decisions/ (one file per decision)
```
