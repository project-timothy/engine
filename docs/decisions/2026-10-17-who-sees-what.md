# Who sees what at a sending agency, and the read log
Date: 2026-10-17
Type: Two-way door

Chunk 9, board item t-agwm-scopes.

## Decision

- **`people` is a resource.** It is who someone is and who looks after them,
  never their money. Member care at a sending agency holds
  `view:people@own` and `view:calendar@own` and nothing else: the people in
  their care and their dates (a visa renewal), never a month, a report or an
  invoice. `start_here` names the people a person may ask about
  (`sees_people`).
- **A tool whose every row is money is offered only to a person some row of it
  could reach** (`MONEY_TOOLS` in core/tools/catalog.py). Member care is never
  offered `expense_reports`, so an empty answer never reads to them as "Mei has
  no expenses". Everyone who could see a row before still has the tool.
- **`[safeguards] log_reads = "always"` writes down every read through the
  door**: who asked, which tool, and whose units' rows reached them (a row in
  no one's unit reads as `ministry`), one JSON line per call in
  `read_log.jsonl` beside the ledger. A tool nobody offered them, and a call
  the tool refused (it answered with an error), are logged as refused. The rows themselves are never written. A read whose line cannot be
  written is not answered.

## Why

Member care is pastoral. An agency wants it to see people and wants every look
on record, and a member care worker should never hear an empty money answer
as a fact about someone.

The log is its own append-only file and not a ledger table, so a read still
takes no lock and waits on no run (the door has always read the ledger
read-only), and no ledger schema changes. It lives in the ledger folder, so
each run's ledger commit (`git add -A`, core/ledger/git_commit.py) carries it
into the ledger's history and backup. It reuses `append_event_line`, which now
takes the file name.

`log_reads` defaults to `never`, the one safeguard default that is not the
strictest: a log is a record, not a control, and a ministry that has not asked
for one keeps its door exactly as it was (every business tenant, Grace, every template).

## Not yet

Reading the log through Tim (compliance asking "who looked at Mei's month?").
A small next step once an agency is live.
