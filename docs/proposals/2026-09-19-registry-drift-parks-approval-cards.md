# proposal: the project registry's drift report parks approval cards

Candidate: c9954464

Lens 17 (`registry`, `registry-drift`) has carried the same INFO since
**2026-09-08** — **11 nights** on tonight's report, the longest-open item on
the checklist. The detail has not moved in all that time:
`project-registry-drift-2026-09-13.md` lists 5 items, and lens 19's
`long-lived` line (`c9954464`) asks the triage to name the automation. The
2026-09-18 triage named it: *port the Sunday sync into the engine so drift
parks cards, not a file nobody opens.* This proposal narrows that naming to
the half that actually retires the finding, and says why.

**The finding is not "the sync is broken".** The sync runs; the file is
fresh; the lens is right. The finding is open because a drift item is a
CHORE WITH NO HOME: a markdown bullet in a file whose only reader is a lens
that reports it. Every other chore in this system has a home — an approval
card with a proposed act, decided once, remembered forever.

## Design

**The mechanism.** A new agent, `core/agents/projects/`, with one job,
`projects/drift`, in the existing park-then-execute idiom (the shape
`ap.record_direct_payment` and `ap.w9_file_and_flip` already use):

1. **Parse, in pure code.** `core/agents/projects/drift.py` reads the newest
   `project-registry-drift-*.md` beside `[projects].registry_path` and
   returns one item per bullet, each classified into exactly one kind:
   - `qbo-project-missing` — *"needs_qbo_project=true but no QBO Project
     (present in the file store)"*;
   - `folder-missing` — *"tracked but no file-store folder"*;
   - `one-source` — *"appears in only one source. Review before adding to
     registry."*
   A bullet matching no kind is carried as `unclassified` and reported, never
   guessed at and never silently dropped: an unreadable drift report must not
   look like a clean one.
2. **Park one card per item**, `projects.registry_drift`, keyed on
   `(pn, kind)` — not on the report filename, because next Sunday's report
   repeats the same item under a new name and the owner must be asked once,
   ever. **A DECIDED card is a permanent answer**, approved or rejected, the
   `_direct_payment_explained` rule (#287): a rejection means "this one is
   fine as it is" and the item never cards again. That is the whole
   difference between a card and the file: a file cannot remember an answer.
3. **Execute on approval, and prove it.**
   - `qbo-project-missing`: create the QBO Project — in the API a Customer
     carrying `IsProject` under the customer named on the card — then
     **RE-READ it**. `projects.qbo_project_created` is recorded ONLY when the
     readback returns a project with that name. A readback that does not
     records `projects.qbo_project_unverified`, names the PN, and stops.
   - `folder-missing`: `mkdir` of the project folder under
     `[projects].folder_root`, inside an `allowed_paths` carve-out. Create
     only — never a move, never a delete, never a rename.
   - `one-source`: decision only. The engine **never writes the registry
     TOML** (the build-3 `vendors.toml` precedent: emit the diff, the owner
     applies it). An approved card emits the one-line diff and says where it
     goes; a rejected card closes the item for good.

**Why the readback is load-bearing.** 2026-08-03: the QBO API accepted a
Preferences update setting `BookCloseDate` and silently ignored it, and an
earlier no-op write had "verified" it. Nothing in this repo has ever created
a QBO Customer or Project — `core/adapters/qbo.py` has `create_bill`,
`create_purchase`, `create_bill_payment` and their getters, and no customer
entity at all — so the write is unproven until one live create is read back.
The build starts with that probe on a single PN and can kill the row in an
hour if Intuit refuses `IsProject`.

**Why NOT port the sync, which is what the naming said.** The sync is a
weekly job in the tenant's own tooling repository (Sunday), and porting it moves
code without retiring one nightly line: the drift it reports is REAL — the
QBO Project genuinely does not exist, the file-store folder genuinely is not
there. Moving the generator into the engine would produce the same five
items from the same three sources and leave them in the same unread file.
The file is not the problem; the absence of an act is. The drift report is
already a stable contract (lens 17 parses it today), so it is the input, and
porting the sync becomes a later, optional row that changes nothing about
this one.

**The one real cost.** `core/` may not import `auditor/` (the package
independence lint), so the engine grows its own parse of a file lens 17
already parses, and two parsers of one format can drift. Acceptance pins it:
the engine's item list must equal lens 17's, item for item, on the live
report, before any card parks.

**Off by default.** `[projects].drift_cards = false` ships with the row, and
the `scripts/engine-ap-daily.sh` line lands inert — phase-7 rule one, and
the #294 precedent: the owner flips the flag on a coding day, with the first
run's cards in front of him.

**What it must never do.**

- Never write `project-registry.toml`, and never write anything under the
  auditor's report or store trees.
- Never delete, move, or rename a folder; the folder act is `mkdir` and
  nothing else.
- Never create a QBO Customer that is not the Project the card names, never
  rename or merge one, never touch a transaction, an account, or money.
- Never act without an approved card, and never in a shadow run.
- Never re-ask a decided item, whatever the next report calls its file.
- Never invent a PN. An `unclassified` bullet is reported, not acted on.

## Failing eval

`evals/proposals/test_registry_drift_parks_approval_cards.py`, two
assertions:

1. **`test_every_drift_item_parks_one_card_and_a_decided_item_never_reasks`**
   — a drift report carrying one item of each kind yields three items with
   those kinds; each parks exactly one `projects.registry_drift` card keyed
   on `(pn, kind)`; passing those keys back as already-decided yields NO
   cards, including for an item that reappears in a later report under a new
   filename. This is the memory a file does not have.
2. **`test_a_created_project_is_recorded_only_when_the_readback_shows_it`** —
   against a fake client that accepts the create and returns nothing on
   readback, execution records no `projects.qbo_project_created` event,
   reports the PN unverified, and writes no registry line. The 2026-08-03
   contract, written before the code exists rather than after it fails.

**Why it fails today.** `core.agents.projects` does not exist; the drift
report's only reader is lens 17, which reports it. The eval fails at the
import, by design, and stays red until the build lane makes it pass and
moves it into the agent's own `evals/` tree.

## Issue

**Goal.** A project-registry drift item is a card the owner answers once
instead of a line in a file nobody opens: each item parks one
`projects.registry_drift` card, an approved card performs the act (QBO
Project created and read back, or the project folder created), and a decided
item never asks again.

**Acceptance.**
- The engine's parse of the live `project-registry-drift-*.md` yields the
  same item list lens 17 reports, item for item, before any card parks.
- One item of each kind parks exactly one card keyed on `(pn, kind)`; a
  second run parks none; a rejected item never re-asks under a later
  report filename.
- A live probe proves a QBO Project can be created and read back on one PN.
  If Intuit refuses, the row closes with that recorded and the QBO half
  stays an owner UI act; the folder and decision halves still ship.
- An unverified readback records no created event, names the PN, and does
  not retry silently.
- No write to `project-registry.toml`, none under `_auditor/`, and no
  delete, move, or rename anywhere in the diff.
- The job is a noop while `[projects].drift_cards` is false, and the daily
  script line lands with it false.

**Touches.** `core/agents/projects/` (new: `drift.py` pure, `jobs.py` card +
approval check + execution), `core/adapters/qbo.py` (create + read a
Customer carrying `IsProject`), `core/engine/config.py` (`[projects]`:
`registry_path`, `drift_glob`, `folder_root`, `drift_cards`),
`tenants/<tenant>/tenant.toml` (the table, flag false; `folder_root` in
`allowed_paths`), `core/agents/projects/brief.md` (invariant 6, same PR),
`scripts/engine-ap-daily.sh` (one job line).

**Size.** M. The parse and the cards are a morning; the QBO probe comes
first and can kill half the row in an hour.

**Depends.** Nothing blocking. The approval queue, the approval-time checks
(7.1) and the gated-write idiom all exist.

**Door.** Two-way. The lane is off behind a tenant flag; the acts it takes
are a folder that can be deleted by hand and a QBO Project that can be made
inactive in the UI; no money moves, nothing is filed, and no book of record
is written.
