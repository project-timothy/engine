# The auditor (Vince-2) — design

Status: BUILT 2026-07-20 (steps 1-5; PRs #63-#66); ADVISORY VOICE +
OWNER TRIAGE built 2026-07-21 (#73) — the output contract is complete.
Nineteen lenses live (lens 9, context, added 2026-07-22; lens 10, host, added 2026-08-18; lens 11, po-watch, 2026-08-24; lens 12, vendor-1099, 2026-08-26; lenses 13-18, the 2026-09-04 catch-up from the retired bookkeeper-auditor's inventory; lens 19, recurrence, 2026-09-10: the auditor re-reads its own store for repeats and names the automation that would retire each, the first piece of the engine's self-improvement loop),
The host's nightly job runs it at 02:00; the
report carries the Advisory section (model counsel over lens-computed
facts, deterministic fallback voice, year-end-CPA appendix) and
`tenants/<slug>/triage.toml` holds the owner's standing decisions. Step 6
(one week of nightly reports, then severity tuning with the owner) is in
progress. Successor to the retired
bookkeeper-auditor, built ground-up per the owner's decision ("don't port")
and his framing of what it must be: **an independent application checking
on the workflows.**

## The one principle everything follows from

A checker that shares the worker's code shares the worker's bugs, and a
shared bug passes both. So independence here is structural, not aspirational:

1. **Recompute, never reread conclusions.** The auditor derives its own
   answers from ground truths — the ledger's SQLite and event log read with
   its OWN queries, the delivered workbook opened with its own reader, the
   mailbox listed with its own client, QBO counted with its own fetch — and
   diffs those derivations against what the engine produced. It never calls
   the engine's matchers, parsers, or view builders to decide what SHOULD
   be true.
2. **A hard import boundary, enforced like the tenant boundary.** The
   auditor lives in a separate top-level package (`auditor/`, sibling of
   `core/`) and may import NOTHING from `core/`. A new CI lint
   (`auditor_independence_lint`, same spirit as the bleed-through lint)
   fails the build on any `from core...` / `import core` under `auditor/`.
   Deliberate duplication — its own tiny QBO fetch, its own Graph listing,
   its own SQL — is the cost of a checker worth having, and it is small.
3. **It writes nowhere the engine writes.** Own CLI (`uv run auditor run
   <tenant>`), own launchd job, own data store (`.auditor/<tenant>/`), own
   report folder. It never touches the engine ledger, the landing folder,
   the filing tree, or the workbook. Its output is questions for the owner,
   never mutations of the record.
4. **Same repo for now** (CI, pins, and deployment ride along), extraction
   to its own repository stays a two-way door once the lint proves the
   boundary holds. Flagged as the one packaging decision the owner may
   want differently.

## Coordination points (the only two, both documented)

- **QBO token rotation:** Intuit rotates the refresh token on use. The
  auditor's independent fetch honors the same advisory lock file
  (`~/.qbo-tokens.lock`) and runs at 02:00, six hours from the engine's
  08:00, so contention is a non-event in practice and safe under the lock
  regardless.
- **Mail token cache:** same story with the keychain MSAL cache; msal's
  cache serialization plus the hour gap covers it.

## Lenses (v1, all nightly at 02:00)

1. **Heartbeat.** Did the 08:00 engine run fire and exit clean (runs table
   timestamps, not the log file)? Did the ledger back up to the remote in
   the last 25h (its own `git log` of the ledger repo vs origin)? Is the
   QBO refresh token younger than its expiry horizon?
2. **Filing coverage.** Walk the landing folder and `_archive` itself;
   every file must have a disposition in the event log (recorded, routed,
   skipped, archived, or a pending approval). A file with no story, or
   top-level items older than the decision window, is a finding. This is
   the lens that catches "something arrived and nothing noticed."
3. **Mail coverage.** List inbox messages-with-attachments for the window
   with its own Graph client and reconcile counts against the engine's
   saved/denied/filtered/dup tallies. Counts only for denied mail — the
   auditor inherits the privacy rule: no identifiers from denied senders
   in anything it persists.
4. **Book integrity.** Recompute every workbook row from `ap_invoices`
   with its own SQL, open the delivered `Ledger_2026.xlsx` with openpyxl,
   and diff: row set, amounts (integer cents), statuses, payment fields.
   Any drift means the delivery view lies about the ledger — the exact
   failure invariant 1 forbids.
5. **Status coherence.** For every row: current status equals the last
   history entry; settled rows have no later flips; every Paid row carries
   evidence (payment date, reconcile event, or an owner note); every
   committed row is absent from payable classification.
6. **Approval hygiene.** Pending cards older than N days; approved
   `ap.qbo_push_batch` coverage fully consumed (each covered row has a
   bill id or a parked mapping/duplicate card); resolved cards whose
   follow-through never happened.
7. **QBO consistency.** With its own fetch: every stored `qbo_bill_id`
   exists in QBO with the row's amount; every reconcile-flipped row's
   evidence transaction still exists; engine-authored records match what
   the events claim was written. Drift in either direction is a finding.
8. **Timesheets.** Every filed submission has its recorded event and its
   payroll-hours card; no submission stuck unrecorded in the landing tree.
9. **Context** (added 2026-07-22). The owner's canonical context system:
   the context repo is committed and pushed (>48h unpushed is CRITICAL);
   every generated shim in `[auditor.context].shims` exists, carries the
   v1 stamp, matches its body hash (a hand-edit is CRITICAL — those edits
   die at the next regenerate), and was generated from the repo's current
   HEAD. A shim the lens cannot read (a dataless cloud placeholder,
   EDEADLK only) is a third state, `unverifiable`, never drift: with
   `sync_roots` configured, every unreadable shim under one synced tree is
   ONE INFO `unverifiable` finding naming the tree and the count (2026-10-02,
   proposal 9ef65b3a); outside every root, or with no roots configured, it
   stays today's per-file WARN `cloud-only`. Persistence escalates through
   lens 19, never a second timer here. The focus file's latest commit is younger than the window and
   the file stays under `focus_max_bytes` (default 25,000, the context
   generator's soft cap; WARN `over-size`);
   retired context locations stay dead. The month's first audit deals one
   INFO review card. Local only (filesystem + git), runs under
   `--local-only`. Config: `[auditor.context]` in tenant.toml.
10. **Host** (added 2026-08-18). The machine under the back office: free
   space on the data volume against WARN/CRITICAL floors (one item that
   escalates; on macOS the floors measure the APFS container's free space
   from `diskutil info -plist`, not `df`, because `df` excludes the
   purgeable local-snapshot space macOS reclaims by itself, and the finding
   names both numbers: docs/decisions/2026-09-15-disk-lens-reads-container-free.md); Time Machine has a destination, it is mounted, and the newest
   completed backup is inside the window (`tmutil`, the OS's own answer);
   every expected volume (the archive tier) is mounted; no evicted item
   (iCloud `.name.icloud` placeholder or File Provider dataless entry)
   inside the trees the engine reads, inspected with `lstat` only so the
   lens can never trigger the materialization it is warning about; entries
   the walk could not list or stat are counted as `walk-incomplete`, and a
   failing `tmutil latestbackup` is `probe-failed`, distinct from a
   destination with no backup yet. Written
   after the 2026-08-18 storage review: 20 GB free, no Time Machine ever
   configured, two eviction incidents on record. Local only, runs under
   `--local-only`. Config: `[auditor.host]` in tenant.toml.
11. **PO watch** (added 2026-08-24, issue #111). Every customer PO the mail
   fetch archives reaches its two homes: the canonical PO folder (one
   customer subfolder each, issue #158) and the register's Open-POs sheet.
   Local only. Config: `[auditor.po_watch]`.
12. **Vendor 1099** (added 2026-08-26, docs/w9-1099-design.md build 2). The
   registry's 1099 picture and QBO's `Vendor1099` flags agree. External.
   Config: `[auditor.vendor_1099]`.

The six lenses below are the 2026-09-04 catch-up: the checks the retired
bookkeeper-auditor had that this one lacked (legacy inventory of
2026-09-03). All local, all read-only, each with an `[auditor.<name>]`
section whose `enabled` DEFAULTS TO TRUE (an absent section is the default
configuration, not an opt-out); a lens that needs a path stays out of scope
while the path is unset.

13. **Materiality** (old A-AP-005). An AP row over the floor ($5,000, the
   PRD's open question 4) AND over 3x the vendor's trailing-365-day MEDIAN
   (median, so one prior outlier cannot lift the floor; at least one peer
   inside the window; void and cancelled rows neither fire nor count) is
   `amount-outlier`, WARN, naming the row, the median, and the multiple.
   The subject is (vendor, invoice number, amount to the cent), never the
   row id, so the fingerprint survives re-runs and never covers a different
   amount. Acknowledge a confirmed amount with a DATED MUTE on the
   fingerprint printed in the finding (`[findings] muted = [{key = "<fp>",
   since = "YYYY-MM-DD", why = "..."}]`): the checklist mechanism, chosen
   over `[advisory.acknowledged]` because this is a checklist item with a
   fingerprint, not an advisory subject; the dated form ages out after 90
   days by design. Config: `[auditor.materiality]` (floor_dollars,
   multiple, window_days).
14. **Check gaps** (old A-BR-005, re-founded on the ledger). The paper-check
   series is rebuilt from the auditor's own reads: every 3-to-5 digit run
   in a settled AP row's check ref (lookarounds keep a 7-digit vendor id
   from reading as a check), expense reimbursements' `instrument_ref`, and
   the bank's own sightings (`ap.reconcile.unknown` events carrying a
   check ref count as OBSERVED, so a cleared-but-unrecorded number is
   named by lens 15, never as a gap too). A series (`1xxx`, the thousands
   band) with `min_observed` numbers is a sequence; every run of missing
   numbers between two observed neighbours is one `check-gap` WARN naming
   the numbers and both neighbours' dates, when the upper neighbour is
   inside the 90-day window. An owner-hand-written check that never touched
   the ledger is exactly what this catches. Bank-assigned electronic ids
   share the shape and gap on design, so `series` names the paper series to
   watch. Config: `[auditor.check_gaps]` (window_days, min_observed,
   series).
15. **Reconcile unknowns.** Money that left the bank and matched no ledger
   row is recorded by the engine as `ap.reconcile.unknown` and was never
   mentioned again (the 2026-05-19 lesson: an unlogged payment, not a
   search miss). Every such clearing inside the 30-day window is
   `unknown-clearing`, WARN, once each, keyed by the bank-side transaction
   id (the engine's own event key), naming payee, amount, date, and check
   ref. A clearing logged more than once reports its NEWEST event: the QBO
   record behind it can be edited between runs (Purchase 304, 2026-09-16),
   and the oldest event then names a figure that never cleared. The
   fingerprint stays subject-only, so a refreshed detail line keeps the
   item's mutes and acknowledgments. Payees in the engine's ignore list
   never produce the event. One clearing is excluded whatever the log says:
   a `Purchase:<id>` whose id is an `expense_report.qbo_purchase_id` in this
   ledger is the record the ENGINE wrote for one of its own expense reports
   (issue #281), so the report is the ledger row that clearing matched. The
   lens re-derives that join itself rather than reading an engine verdict —
   an append-only log cannot retract, and auditing independently is the
   point.
   Config: `[auditor.reconcile]` (unknown_window_days).
16. **Triage.** Two checks. The headless triage wrapper writes its own note
   when it cannot run (first line ends `headless run SKIPPED` / `FAILED`,
   the strings in the tenant's wrapper script); three such markers went
   unread in 2026-09 (09-02, 09-03, 09-04). The newest note in the
   configured notes folder is read: a marker is `triage-skipped` /
   `triage-failed` (WARN) carrying the wrapper's reason, resolved the
   moment a real note is newer; the folder is explicit config (unset = no
   headless triage, out of scope; set and absent = `tree-missing`). And
   dated triage.toml entries older than `ack_max_age_days` (90) are
   `acknowledgment-aged` (INFO, once, "re-confirm or delete"); see Owner
   triage below. Config: `[auditor.triage]` (notes_dir, ack_max_age_days).
17. **Registry.** The project registry TOML named in
   `[auditor.registry].path` is written by a sync outside the engine and
   read by lens 18 as the list of real PNs. Its mtime older than
   `max_age_hours` (168) is `registry-stale` (WARN); the newest
   `project-registry-drift-*.md` beside it listing items is
   `registry-drift` (INFO, one item whose detail names the file and the
   items); the file absent is `registry-missing` (WARN).
18. **Projects** (old A-PJ-001 and its canonicalizer). Every PN referenced
   by the ledger (the P-number in an AP row's `gl_account` and its
   `project` field, `expense_line.project`, and `timesheets.recorded`
   events' line projects) resolves through `auditor/pn.py` (four written
   forms to `PYY_NNNN`; nicknames from `[auditor.projects].nicknames`,
   tenant data never code; OVERHEAD and MULTI sentinels) and must exist in
   the registry TOML's `[[project]].pn` list. A PN not registered is
   `orphan-pn` (WARN, one per PN, naming where it was seen); a project
   string that resolves to nothing is `unresolved-project` (INFO). PN
   identity is the money boundary: per-project COGS drives the profit
   share and the year-end picture.

## Output contract: the checklist, not the alarm

Owner requirement (2026-07-17, from living with the previous auditor): it
"was just reminding me daily, again and again, of the same things." The
report is therefore a RUNNING CHECKLIST the auditor remembers, never a
daily re-announcement:

- Every finding has a stable fingerprint (lens + subject + condition).
  The auditor's own store tracks first-seen, last-seen, and state
  (OPEN / RESOLVED). A finding seen yesterday and still true today is the
  SAME checklist item, silently carried forward — never re-alarmed.
- The nightly report has exactly three sections, in this order:
  1. **NEW since the last report** — called out loudly. The only section
     that asks for the owner's attention.
  2. **The open checklist** — everything he still needs to run down, one
     line each with first-seen date and severity, oldest first. Stable
     from day to day; reads like a to-do list, because it is one.
  3. **Resolved since the last report** — items whose condition stopped
     being true. The auditor re-verifies every open item every night and
     closes what reality has fixed; no manual check-off needed. A quiet
     record that the list is shrinking. Only a lens that actually RAN can
     resolve its items (honesty audit 2026-09-03): a lens that crashed or
     was skipped under `--local-only` leaves its open items open and
     unlisted under resolved, and the report opens with a "Partial
     coverage" line naming every lens that did not run.
- The checklist store commits only after the report is on disk: a failed
  write rolls the night's reconcile back so the next good night still
  announces its items as NEW, and `--no-report` is a dry run that previews
  without consuming an announcement.
- An empty report says so in one line. Silence is never the signal (a
  report that failed to generate is itself a CRITICAL heartbeat condition
  the next night). A configured tree the lens cannot find (landing folder,
  PO archive or home, Timesheets tree) is a `tree-missing` WARN, never a
  clean section; an unconfigured tree stays out of scope.
- Severity: INFO / WARN / CRITICAL. CRITICAL v1 = money-shaped drift
  (book diff, QBO mismatch, Paid without evidence) or a dead heartbeat.
  A CRITICAL item still open after 3 nights gets one aging bump back into
  the NEW section, not a daily repeat.
- Report file: `Financials .../05_Reports/_auditor/audit-YYYY-MM-DD.md`,
  the auditor's ONLY delivery surface. No external sends in v1; a
  notification hook is a later, owner-approved addition.

## The advisor voice (CPA lens)

Owner requirement (2026-07-17): beyond checking, the auditor should
ADVISE where it makes sense — a CPA's eye on the books, not just a
proofreader's.

- The nightly report gains a fourth section, **Advisory**, written in
  plain professional counsel: coding drift ("this vendor was coded three
  different ways this quarter"), treatment questions (owner expense
  reimbursements vs. loans-payable entries — already an open question in
  this book), aging observations, close-readiness, and a running
  **"for your year-end CPA"** appendix (1099-relevant vendor totals via
  the registry's tax_entity grouping, W-9 gaps, accrual notes) so the
  January handoff is a folder, not an archaeology dig.
- Boundary, per invariant 2's division of labor: the deterministic
  lenses COMPUTE every fact; the advisory voice may use an LLM to reason
  over and draft from THOSE COMPUTED FACTS ONLY — it makes no API calls,
  reads no fresh data, and its output is prose in one report section.
  Advice never creates checklist items, never sets severities, never
  touches a book. Classify, draft, flag: the exact three things a model
  is allowed to do here.
- Advisory topics are triageable like anything else (mute a topic in
  triage.toml and it stays muted).
- Which model drafts is tenant policy, not a constant in the drafter
  (2026-09-15, phase 7 row 7.12): `[llm.jobs].draft_advisory` names a tier
  in `[llm.tiers]`, and `auditor/advisory/llm_client.py` (the auditor's own
  vendored copy of the model seam, since it may import nothing from `core/`)
  speaks to it with no tools and one turn. Any failure renders
  `fallback_counsel` and the Advisory section says so in one line with a
  short reason label; `--local-only` never constructs a client at all. A
  tenant naming no `[llm]` tables keeps the Claude Agent SDK seat path
  unchanged.

## Owner triage: dated entries (2026-09-04)

`triage.toml` decisions can carry dates. A `muted` entry may be a table
instead of a bare string, and an acknowledgment's value may be a table
instead of a bare answer:

- `{key = "<lens | lens/condition | fingerprint>", snooze_until =
  "YYYY-MM-DD"}` is a DEFER: the item is muted while the date is in the
  future (an open one resolves visibly once, as any mute), and on that
  date it comes back and is announced as NEW with "(snooze expired)"; the
  entry is then inert (delete or re-date it). Dates are judged on the
  tenant's local calendar date, the one the report carries.
- `{key = "...", since = "YYYY-MM-DD", why = "..."}` on a mute, or
  `{answer = "...", since = "YYYY-MM-DD"}` on an acknowledgment, is a dated
  decision: it keeps applying, and once older than
  `[auditor.triage].ack_max_age_days` (90) the triage lens reports it once
  as `acknowledgment-aged` ("re-confirm or delete"): bump the date to keep
  it, delete the entry to resume surveillance. One line per entry, never a
  flood of un-muted items.
- An entry with no date keeps the original permanent semantics; an
  unreadable date reads as no date (silence stays silent).
- The real year-end CPA remains the authority the advisor prepares FOR;
  the advisor's success metric is how little that engagement costs.

## Learning loop

Every finding class ships with a fixture eval in `auditor/evals/` before
the check merges (TDD, invariant 8 applied to the checker itself). A false
positive in production becomes a regression eval the same week. The
auditor is subject to the same bleed-through lint as everything else: no
tenant tokens under `auditor/`.

## Explicitly out of scope (v1)

Fixing anything (it reports; the engine or the owner acts), AR, payroll
internals (the payroll provider's books are its own), tax positions, the old stack's
folder trees, notification channels, and any write to QBO or the mailbox
(the auditor holds read scopes only, enforced by its code never containing
a write call — and the independence lint keeps the engine's write code
un-importable).

## Build plan (starts 2026-07-18)

1. Skeleton: `auditor/` package, CLI, own store, independence lint wired
   into CI (half day).
2. Lenses 1-2-5-6 (pure-local, no external calls) with evals.
3. Lens 4 (book diff) — the highest-value single check.
4. Lenses 3 and 7 (external reads, own clients).
5. Lens 8, the host's nightly job at 02:00, first nightly run.
6. One week of nightly reports, then tune severities with the owner.
