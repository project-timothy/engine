# timothy-engine

A business-agnostic back-office agent engine. Every back-office job is an
idempotent CLI invocation against a git-backed ledger, every agent is a brief
plus deterministic code versioned together, every tenant is pure configuration,
and every past incident is a regression test.

It ships twelve agents and their jobs: accounts payable (`ap`), accounts
receivable remittance (`ar`), month-end `close`, `expenses`, `timesheets`,
`mail`, `deadlines`, `projects`, `policy` (a handbook's rules, each in force
only on a person's yes), a weekly `brief`, approval `routing`, and `demo`; run
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
| `uv run engine tracker <tenant> <person> [--domain d]` | A token for one person's phone tracker app; the box keeps their drives as miles and times only |
| `uv run engine receipt-link <tenant> <person> [--domain d] [--revoke]` | A person's standing receipt page for their home screen (it only adds photos to their own receipts), or `--revoke` to retire it |
| `uv run engine people add <tenant> <list> [--check]` | New people from one list, one a line (`Ivy Chen = missionary in Thailand, ivy@example.org, looked after by Ann Lee`): each in authority.toml with their unit, address and zone, filers in the expense tree, each named leader gaining them; one bad line refuses the whole list; `-` reads stdin |
| `uv run engine calendar-link <tenant> <person> --revoke` | Retire a person's calendar feed link (their setup's calendar step makes a new one) |
| `uv run engine notices <tenant>` | The emails the box should send (a month ready, a yes waiting), one JSON line each, for `doorkeeper send-notices` |
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
| `uv run engine talk <tenant> --as <person> [--say "..."]` | Talk to Tim in the terminal as one person: his own voice over the read tools (`core/talk`); off until the tenant's `[llm.jobs]` names a tier for `talk` that returns one plain reply (never `claude_agent_sdk`); a hard question steps up to `talk_hard` by rule (`core/talk/speeds.py`) |
| `uv run engine spend <tenant> [--json]` | What the ministry's models cost this month, by job type, with the cap and Tim's soft line: one plain line, or JSON for the build board |
| `uv run engine capabilities [--format json]` | What Tim can do today, built from the code: the tools, the setup questions, each lane in one plain sentence, and what is not built yet |
| `uv run engine board-pack <tenant> --as <person> [--period 2026] --out <dir>` | A ministry's statements for a span (activities, financial position, the program and management split), as a page and a PDF, read through the same tool Tim answers with |
| `uv run engine tax-packet <tenant> --as <person> [--person <missionary>] [--year 2026] --out <dir>` | A missionary's tax packet for a year as one page and a PDF: income from each payer, Schedule C, self-employment tax, the days abroad drafted from the bank, next year's estimates. Sends nothing |
| `uv run engine donor-letters <tenant> --as <person> [--year 2026] --out <dir>` | A year's donor letters as pages and PDFs: each donor's year-end giving statement, an acknowledgment of each gift of $250 or more, a thank-you for each donor-advised fund grant. Sends nothing |
| `uv run engine cpa-export <tenant> --as <person> --year 2026 --out <dir>` | A year's books for an accountant: the journal as Beancount-format text, the trial balance as CSV, and the 990 and 1099 starting numbers, each read back and checked against the books |
| `uv run engine room pack <room folder> --out <file>` / `room restore <file> --into <new folder>` | A house's room, packed to move: every file's hash and every table's count in a manifest; a restore lands only where all of them match, never over a room that exists (core/engine/room.py) |
| `uv run engine faq [--format json]` | The questions people ask about Tim, with Tim's answers; held to the door's tools by a test (docs/tim-faq.md) |
| `uv run engine interview new --name <n> --email <e> --by <you> --base https://<box>` / `interview list` | A missionary's own setup questions: a one-time link she answers on her phone; her yes writes the ministry file the installer takes (`list` says where each stands) |
| `uv run engine rules <tenant>` | The ministry's policy rules, highest layer first, each in Tim's words with the page it came from; a lower layer may only make a rule stricter (core/policy) |
| `uv run engine pack check <folder>` | Whether an agency pack can set up a ministry: every amount cited, every citation and quote still reading in its copy, its rules loading against its own roles (core/pack) |
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
core/talk/      Tim's own voice: the conversation held on the box, its files, its cost meter
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
