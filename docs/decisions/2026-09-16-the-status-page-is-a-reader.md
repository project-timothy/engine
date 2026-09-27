# The status page is a reader: `engine status-page`, and where the file lands
Date: 2026-09-16
Type: Two-way door

Phase 7 row 7.24 (#233). One self-contained HTML file: the last run of every
job, the approval cards still pending grouped by type, and the NEW section of
the newest audit report. Opened as a file, or served on the tailnet. Nothing
schedules it, nothing serves it from inside the engine.

## The command is `engine status-page`, not `engine status`

`engine status <tenant> <invoice-ref> --scheduled --check <n>` already exists
and is the owner act that puts a check number on a row before it clears
(decision 2026-09-12, check-ref lands at scheduling). That name is in the
owner's manual, in muscle memory, and in the hand-check lane's instructions,
so the page takes the second name rather than the write-back giving up its
first. `status-page` also says what comes out: a page, not a status.

## Where the file lands

Default: `<[close].report_dir>/_status/status.html`, overridable with `--out`,
or `--stdout` to print. A tenant that configures no report tree is refused
with the flag named, never guessed at.

`[close].report_dir` is the tenant's REPORT TREE, not a close-only folder: at
the live tenant it is `05_Reports`, with the auditor's `_auditor` and the
sweep's `_qbo-sweep` already sitting inside it. `_status` joins them as a
sibling.

The obvious alternative, writing into `[auditor].report_dir` beside the
report the page quotes, is refused on the auditor's own rule, stated in the
tenant file itself: the auditor "never writes anywhere the engine writes",
and the converse holds. The auditor's folder is an INPUT to this page and
stays read-only to the engine, or the independence that makes the nightly
report worth reading starts to blur.

## It reads through a `mode=ro` connection, never `Ledger.open`

`Ledger.open` is a write: it runs `ensure_repo` (creating the git repository
if it is absent) and `migrate` (applying every pending migration). A page the
owner refreshes must never be the thing that migrates a ledger, and a page
rendered against a copy must never leave a git repo behind in the copy. So
this module opens `sqlite3.connect("file:...?mode=ro", uri=True)` and the
driver itself refuses every mutation. "No write path" is then a mechanism,
not a convention: a bug in the renderer raises `readonly database` instead of
touching the ledger. The one file this module writes is the page.

The same choice pays a second time. A ledger behind the current schema is not
migrated on the way past; the page reads what the columns say it has
(`job_records` is absent before migration 6, its retry columns before
migration 8) and renders the rest. A status page that crashes on an old
ledger is worse than one that shows less.

## "Rendered from `job_records`" means job_records plus the run row

The row's wording is `job_records`, and that is where the durable job-time
records and row 7.23's retry chain live. It is not where a run's STATUS or
its TIME live, and it never was: `job_records` carries `run_key`, `run_id`,
a record type and a payload. On the live ledger today it holds 16 rows of two
record types, against 426 runs across 24 jobs, because only a job that moves
a file outside the ledger records anything. A page built from it alone would
show two jobs and no outcome.

So the last-run table is the `runs` row (status, landed, run key, summary)
with the `job_records` rows for that run joined on `run_id`, which is the
column that survives a failure: a FAILED run is recorded under
`<run key>:failed:<stamp>` while its records keep the original key, so
joining on the key would silently drop exactly the rows a status page exists
to show. What the join adds to a row: the retry phrase (`attempt 3 of 3,
exhausted`) and the count of durable records.

Failed rows sort to the top, then newest first. One row per `agent/job`.

## What the page will not show, and why

- **No duration.** The ledger records one timestamp per run (`created_at`,
  when the run landed) and no start/finish pair. Showing a duration would
  mean inventing one or adding a column, and a ledger schema change is the
  owner's one-way door. The page says "landed" and stops.
- **No card bodies beyond one line.** Each pending card renders its
  parameters as one line, values trimmed to 60 characters. Every value goes
  through `core/llm/transcript.redact_text` FIRST (the same redaction the
  model transcripts get: named secret values, key-shaped strings, and
  EIN/SSN-shaped strings), and the trim runs after, so a cut can never leave
  half a secret standing. The page may be served on a tailnet; that is the
  bar it is held to.
- **No JavaScript, no external asset, no new dependency.** Inline CSS in one
  `string.Template` file (`core/engine/templates/status_page.html.tmpl`,
  scanned by the bleed-through lint like every other `.tmpl`), because jinja2
  is not a dependency of this engine and a status page is not a reason to
  make it one.
- **Nothing auto-refreshes.** Re-rendering is one command, and the footer
  names the render time so a stale tab is obvious.
