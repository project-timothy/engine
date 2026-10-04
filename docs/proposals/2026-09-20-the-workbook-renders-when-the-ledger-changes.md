# proposal: the workbook renders when the ledger changes, not once a morning

Candidate: 951aaf6d

Lens 4 compares the delivered sheet against `ap_invoices` and calls any
disagreement CRITICAL, correctly: invariant 1 says the view is a photograph of
the ledger, and a view that lies about the book is the highest-value failure
the auditor can catch. But the photograph is taken **once a day**, at line 134
of `scripts/engine-ap-daily.sh`, and the ledger keeps changing after the
shutter closes. Every owner act between one morning's render and the next
leaves the sheet lying, and the auditor — doing its job exactly right — wakes
the owner with a CRITICAL about it.

This is not hypothetical and it is not rare. From the ledger, the 2026-09-17
window, to the second:

```
run 436  workbook  ok  2026-09-17T12:02:42Z   <- the day's render
#160 Vendor A             13:06:18   'Received' -> 'Scheduled'   (owner)
#161 Vendor B             14:09:38   created, Paid, $2,400.00     (hand backfill)
#162 Vendor C             14:09:38   created, Paid, $1,000.00     (hand backfill)
#163 Vendor B             16:09:17   created, Paid, $4,300.00     (card #101)
#164 Vendor D             16:22:33   created, Paid, $2,000.00     (card #102)
#165 Vendor D             16:22:33   created, Paid, $82,000.00    (card #103)
run 463  workbook  ok  2026-09-18T12:00:14Z   <- the sheet catches up
```

(Vendors and amounts above are illustrative; the real rows are in the tenant's
private triage note of 2026-09-18.) Six writes in the four hours after the render. The delivered sheet was wrong
about $91,700 of recorded money for roughly twenty hours, the 2026-09-18
report carried **six CRITICALs**, a triage night was spent proving they were
not defects, and all six closed themselves at the next morning's render. Row
#159 did the identical thing on 2026-09-14 (created 19:58:35Z, CRITICAL on the
9/15 report, gone on 9/16). Lens 19 has now counted it twice over, as
`book`/`missing-from-sheet` (**7 subjects in 60 days**, this candidate) and as
`book`/`field-drift` (3 subjects, `d41d4067` — row #160's status flip is one of
them). One mechanism, two classes.

**The machinery to fix this already exists and is already correct.** The
workbook job is registered as
`JobHandler(key=_workbook_key, run=_workbook_run)`, and `_workbook_key` hashes
every row's `(id, status, amount_cents, updated_at)` together with the column
layout, the output path and the view version. The engine replays a job whose
key is unchanged. That is why there is no workbook run on 2026-09-12 through
09-14 or on 09-19: the ledger had not moved, so the render correctly did
nothing. The render already knows precisely when it is stale. Nothing asks it.

So this row does not build a renderer, a differ, or a scheduler. It moves one
trigger.

## Design

**The mechanism.** The render stops being a stage in a morning script and
becomes a consequence of an AP write. A new pure helper
`core/agents/ap/view_refresh.py` owns the decision; the AP store owns the
signal.

1. **The store marks the view dirty.** The four methods that mutate
   `ap_invoices` — `insert_invoice`, `update_status`, `record_payment_details`,
   `append_note` — set a per-tenant dirty flag after their commit. Not before:
   a render that reads a half-written transaction is worse than a stale one.
2. **The dirty flag is drained once, at the end of the invocation.** A batch
   that writes five rows renders once, not five times. The drain calls the
   existing `workbook` handler, which recomputes `_workbook_key` and replays
   itself into a no-op if the key has not actually moved. A write that changes
   nothing the view shows (a note, a `qbo_bill_id`) therefore costs zero file
   writes, for free, because the existing key already excludes those columns.
3. **The daily script keeps its line.** Line 134 stays as the backstop and as
   the thing that catches a renderer change (`VIEW_VERSION`) rather than a data
   change. Under this row it becomes a no-op on most mornings, which is the
   point: the sheet is already current by then.

**Why the trigger must sit at the store, not at the run.** This is the part
that decides whether the row works, and it is why the obvious version fails.
Rows #161 and #162 above were written by `store.insert_invoice` *outside any
job run* — the 2026-09-18 triage established this from the absence of events
("a row created outside a job run never has one"; it leaves `ap_status_history`
and nothing else). A post-run hook would have re-rendered after the card
executions at 16:09 and 16:22 and **missed both hand backfills entirely**, which
is two of that morning's six CRITICALs still raised. Every path that writes an
AP row goes through the store. Only the store sees them all.

**The one real cost, named.** The output is an `.xlsx` on the owner's Desktop
that a human opens in Excel, and this row makes the engine write it at
unpredictable moments instead of one predictable one. Two consequences worth
deciding deliberately:

- **A file open in Excel.** Writing under an open workbook does not corrupt the
  owner's session (Excel holds its own copy and warns on save), but the owner's
  unsaved edits are lost on their save-or-discard prompt. This is tolerable
  precisely because invariant 1 already forbids hand-editing the view — it is a
  photograph, and anyone editing it is already outside the contract. Worth
  saying out loud rather than discovering.
- **Write volume.** Bounded by the dirty-flag drain: at most one render per CLI
  invocation, and zero on an invocation whose writes do not move the key. On
  2026-09-17, the busiest AP day in the record, this is four renders instead of
  one. On 2026-09-19 it is zero, same as today.

**What it must never do.**

- Never render inside a transaction, and never before the commit that made it
  dirty. Stale beats torn.
- Never fail an AP write. A render that raises records an anomaly on the run
  and leaves the ledger write standing — the money record is truth, the
  photograph is a convenience, and inverting that is how a delivery view starts
  eating accounting writes.
- Never render in a shadow run (`shadow = 1`), which must leave no artifact on
  the owner's disk at all.
- Never bypass the `WriteGuard`. A `workbook_path` inside a protected surface is
  refused now and stays refused when the caller changes.
- Never write the ledger. The view is one-way, forever.
- Never render more than once per invocation, however many rows moved.
- Never be called from `auditor/`. The auditor reads the delivered sheet as the
  owner receives it; a lens that could trigger a render would be checking its
  own output (the package-independence lint enforces this already).

**What this does not fix, deliberately.** A row written by a *different
process* than the one that renders — a second engine invocation racing the
first — still settles at the next drain. Cross-process coordination is a lock,
this row is not, and the existing key makes a redundant render harmless.

## Failing eval

`evals/proposals/test_workbook_renders_when_the_ledger_changes.py`, two
assertions:

1. **`test_a_write_outside_the_render_job_leaves_the_view_stale`** — a store
   that inserts a row and a store that flips a status each mark the view dirty
   and drain to exactly one render; a batch of five writes drains to one; a
   write that moves nothing the view shows drains to none. This is the contract
   the six CRITICALs of 2026-09-18 would have needed, including the two hand
   backfills that no run-level hook can see.
2. **`test_a_render_failure_never_rolls_back_the_ledger_write`** — against a
   renderer that raises, the AP row is still committed and readable, and the
   failure is reported as an anomaly. The photograph never eats the book.

**Why it fails today.** `core.agents.ap.view_refresh` does not exist; the store
has no dirty flag and the render is reachable only as a scheduled job. The eval
fails at the import, by design, and stays red until the build lane makes it pass
and moves it into `core/agents/ap/evals/`.

## Issue

**Goal.** The delivered workbook stops lying between mornings: any committed
change to `ap_invoices` renders the view once, at the end of the invocation
that made it, so lens 4 has nothing to report and the owner is not woken by a
CRITICAL that will fix itself at 08:00.

**Acceptance.**
- A row inserted or updated outside any job run (`store.insert_invoice`, the
  hand-backfill path) re-renders the view. Replaying the 2026-09-17 window
  produces a sheet that matches the ledger at every point after 16:22:33Z, and
  lens 4 reads it clean.
- A batch of N writes in one invocation renders at most once; a write that does
  not move `_workbook_key` renders zero times.
- A raising renderer leaves the AP write committed and surfaces an anomaly; no
  AP job's exit status changes because of a render.
- A shadow run writes no workbook file.
- `evals/proposals/test_workbook_renders_when_the_ledger_changes.py` passes and
  moves to `core/agents/ap/evals/`.
- `scripts/engine-ap-daily.sh` line 134 still renders when `VIEW_VERSION` moves
  and the ledger has not.

**Touches.** `core/agents/ap/view_refresh.py` (new, pure),
`core/agents/ap/store.py` (dirty flag on the four mutators, after commit),
`core/agents/ap/jobs.py` (drain at invocation end; the `workbook` handler is
reused unchanged), `core/agents/ap/brief.md` (invariant 6, same PR),
`docs/lessons.md` (the render window as a lesson).

**Size.** S–M. The helper and the flag are an afternoon; the care is in the
commit ordering and in proving the shadow and failure paths.

**Depends.** Nothing blocking. The renderer, the idempotency key, the write
guard and the job handler all exist and are unchanged by this row.

**Door.** Two-way. The behaviour is additive — the daily render stays — so
reverting the hook restores today's behaviour exactly, and the worst failure
mode is a workbook written more often than necessary.

**Retires.** Candidate `951aaf6d` (`book`/`missing-from-sheet`, 7 subjects in
60 days) and, by the same mechanism, `d41d4067` (`book`/`field-drift`, 3
subjects) — row #160's status flip at 13:06 on 2026-09-17 is a subject of the
second and a consequence of the same missing trigger.
