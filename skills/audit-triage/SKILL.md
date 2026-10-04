---
name: audit-triage
description: Morning triage loop for the engine's nightly auditor. Reads the newest audit report, diagnoses every new finding to root cause, and closes the loop — code bugs become eval-first fix PRs (never agent-merged), operational issues get executed or handed to the owner, judgment calls become Recommended Actions with a recommendation. MANDATORY TRIGGERS - "triage the audit", "audit triage", "morning triage", "what did the auditor find", "the auditor flagged something", "lens failure", "auditor CRITICAL", or any request to diagnose or act on a nightly audit finding.
---

# Audit Triage — close the loop on the nightly audit

**Phase: Tier 2 (headless) since 2026-09-01; on the runner since
2026-09-17 (phase 7 row 7.17).** The shadow phase (2026-08-11 through
2026-08-30) ran clean across real findings. The host's scheduler fires this
skill each morning through the tenant's headless wrapper, which hands it to
`engine runner run` (`core/llm/runner.py`); interactive invocation still
works and takes precedence (a triage note already written for today makes the
headless run a no-op). **The owner merges every PR — Tier 2 changed the
trigger, not the authority.**

## The tenant overlay

This file is the engine's and ships publicly, so it names no business, person,
or host path. The tenant's overlay does: who the owner is, where the audit
reports and triage notes live, the tenant's config and dev clones, and its
private incident log. Through the runner it arrives in the context bundle as
`tenant_overlay`; interactively, read
`$ENGINE_TENANTS_ROOT/<tenant>/skills/audit-triage-overlay.md`. Wherever this
file says "the owner", the overlay's name is meant, section headings
included. Where the two disagree on a path or a name, the overlay wins; on a
guardrail, this file wins.

## Headless mode: the runner session

At 06:00 there is no chat and nobody is watching. The runner, not a prompt,
holds the rules:

- **The triage note IS the deliverable, and it is your final message.**
  There is no note file to write: the runner takes the last message of the
  session as the note and the wrapper installs it as the day's note. A quiet
  night gets the one-liner; a partial diagnosis gets its findings so far.
  Keep the note-so-far in every message as you work, because a session a
  budget stops keeps the last one. An in-progress note MUST carry the word
  "preliminary" in its first line, and the final message must NOT: the runner
  marks a note whose first line still says preliminary as an unfinished run
  (FAILED, re-run on the next fire), because a session that died mid-way
  leaves exactly that (2026-09-05 through 09-07).
- **Everything you are given arrives in the context bundle.** The audit
  reports and the recent triage notes are quoted to you in full; there is
  nothing to fetch and no Desktop path to read. That tree is TCC-gated for a
  launchd-spawned program and a read there blocked one session for 55 hours
  (2026-09-05); the session now cannot reach it at all, by construction.
- **Your tools are the ones the lane granted, and Python judges every
  call.** Files, `git`, `gh`, and `pytest` are rooted in a worktree the
  wrapper created for you off `origin/main`, already on its own branch: work
  there, commit there, push that branch. A call outside the root, a forced
  push, a push to main, or anything the allowlist does not name comes back
  refused with the reason. Route around it or say so in the note. The merge
  command is not a tool at all.
- **`gh pr create` is gated by `eval_first`.** The rule that used to live in
  this prose is now Python: a PR is refused unless the worktree carries a new
  or changed file under `tests/` or an `evals/` directory AND a `pytest` run
  in this session passed after your last write. The refusal names which half
  failed. When it refuses, the note says so plainly and the fix waits for the
  next session; never route around it.
- **Nothing can be asked.** Anything that would have been an in-chat question
  becomes a Recommended Action in the note. The operational lane narrows to
  exactly the reversible, read-side acts already listed — when in doubt,
  recommend instead of act.
- **Tonight's report is the whole job** (owner, 2026-10-03). Work its new
  findings and the open-checklist items the triage history shows as
  untriaged, nothing else. A defect you notice off the report is not
  tonight's work: give it one Watch list line that starts `off-report:` and
  names the mechanism, with no branch and no PR. The owner files it for a
  coding day. Eleven mornings measured: most of the seat went to off-report
  builds nobody had asked for.
- **One plain argv per call.** The runner refuses shell operators (`&&`,
  `|`, `;`, redirects, quoted SQL in a shell string), so each refusal is a
  wasted turn. Run one command, read its output, run the next. The shell
  takes only the prefixes the lane names; git and pytest take only the
  subcommands and flags the runner lists, and the refusal says which.
- **The note stays under 6,000 characters.** Tomorrow's session reads it in
  full, and so does the owner. Root cause in a few sentences per finding,
  evidence by reference (run id, file and line, PR number), no narrative.
  A quiet night is one line.
- **Bounded session.** The run has a turn budget and a wall clock. One PR per
  finding, the most severe finding first; if the budget cannot cover a fix,
  the note carries the diagnosis and a [NEEDS REVIEW] handoff instead of a
  rushed half-fix.
- **Everything else in this file applies unchanged**, guardrails above all:
  never merge, never touch main, no external sends, no QBO writes.

## Sources

Through the runner every source below that is a REPORT arrives in the context
bundle already; interactively you open the paths yourself.

- **Audit reports (read-only):** `audit-YYYY-MM-DD.md` in the reports folder
  the overlay names. Newest file is tonight's. Read the previous day's too — the triage works the
  delta, and "Resolved since the last report" closes loops from prior triages.
- **Engine:** this repository (lenses under `auditor/`, agents under
  `core/agents/`).
- **Tenant config:** `$ENGINE_TENANTS_ROOT/<tenant>/` (`tenant.toml`,
  `triage.toml`, `vendors.toml`), kept in the tenant's own private
  repository (the overlay names its dev clone). The engine carries no copy;
  a fix to tenant config is a PR there, not here.
- **Prior art, read FIRST:** `docs/lessons.md` (one rule per failure pattern,
  each naming its eval), then the tenant's private incident log with the
  particulars (the overlay names it).
  The 2026-08-11 lens crash was a disease AP intake had already recorded on
  2026-07-09 with a full fix pattern waiting to be ported. Before diagnosing
  fresh, check whether the failure signature already has a lesson — port the
  pattern, cite it.
- **Triage history:** `triage/triage-*.md` beside the audit reports — read the
  last note so items already triaged, PR'd, or handed to the owner are not
  re-worked.

## Protocol

1. **Diff.** Open the newest report. Work "New since the last report" plus any
   open-checklist item the triage history shows as untriaged, and only those (off-report
   defects get an `off-report:` Watch list line, never a PR). Confirm items in
   "Resolved" that a prior triage acted on, and say so — closed loops are the
   point.
2. **Diagnose to root cause.** Read the failing lens or agent code, inspect
   filesystem/git/ledger state read-only, reproduce where cheap. A symptom that
   pattern-matches a known failure still gets its evidence checked — name the
   mechanism, not the resemblance. Below 80% confidence, tag [NEEDS REVIEW].
3. **Classify each finding, three outcomes:**
   - **Code bug** → feature branch off current main, failing eval FIRST
     (invariant 8), fix, full gate (`uv run pytest`, `uv run ruff check .`,
     `uv run ruff format --check .`, bleed-through lint, independence lint),
     PR that adds or extends a lesson in `docs/lessons.md` (the rule, no
     particulars; the particulars go in the PR body and the note). **Never merge; never
     commit to main.** The owner merges.
   - **Operational** → reversible, read-side acts (hydrate a file, re-run a
     read-only job) execute now and get reported. Anything else — Finder pins,
     UI acts, QBO writes, config with money meaning — is a Recommended Action
     for the owner with exact steps.
   - **Owner decision** → Recommended Action: the options, one recommendation,
     the reasoning. No hedging.
4. **Record.** The note is your final message through the runner, and
   `triage/triage-YYYY-MM-DD.md` beside the audit reports when you are working
   interactively (create `triage/` if absent; the subfolder keeps the morning
   brief's `audit-*.md` glob clean). Either way it is the same note, with the
   same sections: **New findings**, **Root cause**,
   **Actions taken** (PR links), **Recommended actions for the owner**,
   **Recurring patterns**, **Watch list**. **Recurring patterns** (owner,
   2026-09-10) is the self-improvement hook. Start from the report's
   `recurrence` lens lines (lens 19: `long-lived` and `class`, each with a
   candidate or "name one"): name the unnamed ones, then add repeats the
   lens cannot see (the same owner act every week, the same hand fix after
   every deploy). It is one bullet per thing that has
   now happened more than once (the same finding on consecutive nights, the
   same owner act every week, the same hand fix after every deploy), each
   naming the automation that would retire it. The morning brief lifts the
   first bullet as its item 2, so write the most valuable repeat first and
   keep each bullet under 25 words; write "None spotted." when nothing
   repeats. The **Recommended actions for the owner** bullets are lifted as
   item 1 the same way: first bullet = the one act that unblocks the most.
   A quiet night still gets a one-line note — the record that
   the loop ran is part of the loop.
5. **Deliver.** End with the summary in chat: what broke, why, what shipped,
   what the owner owes (merges, clicks, decisions). Lead with the nuggets.

## Proposal PRs: the eval budget

Lens 19 counts the repeats and names the automation that would retire each
one. A named candidate that nobody builds is a note that repeats forever, so
the night's spare eval budget turns ONE of them into a proposal: a design
note and a failing eval, no implementation. The owner merges the eval as the
decision, and the build lane takes the row from there.

**The budget.** At most one proposal per session, and only when no fix PR
was opened tonight (a fix in the worktree is a source change, and the gate
refuses a proposal that carries one). A night with a code bug spends its
budget on the bug; the candidate waits.

**When.** The report's recurrence lines end with `candidate: <text> [<id>]`.
Only a NAMED candidate qualifies: a line that says "name one" gets named in
Recurring patterns first and can be proposed on a later night. Skip it
entirely when a note in the bundle already records a proposal for that id,
or when `docs/proposals/` already holds one: **a candidate is proposed once,
ever.** Skip it too when the sentence says the automation already exists or
is already on the board (several candidate sentences record what was built
and when). A repeat under an automation that already shipped is a bug report
or a threshold that is wrong, so it belongs in Root cause or Recurring
patterns, never in a proposal.

**What to write**, and nothing else:

1. `docs/proposals/<YYYY-MM-DD>-<slug>.md`, which is also the PR body. It
   opens with a title line, then `Candidate: <id>` on its own line, then
   these sections: `## Design` (the mechanism, the inputs, the approval
   shape, what it must never do), `## Failing eval` (what the eval asserts
   and why it fails today), `## Issue` (the board row in the lane-issue
   shape: Goal, Acceptance, Touches, Size, Depends, Door).
   **The note is published with the engine**, so it names mechanisms only:
   never a vendor, person, customer, amount, invoice or check number, project
   number, or host path, and never a tenant's file by its tenant's name
   (`tenants/<tenant>/triage.toml`, not the real slug). The particulars that
   made the case go in tonight's triage note, which stays private, and the
   proposal cites that note by date.
2. ONE eval at `evals/proposals/test_<slug>.py`. The top-level `evals/` tree
   is outside pytest's `testpaths` on purpose: the eval fails by design, and
   merging it must never turn the suite red.

Then run it (`pytest evals/proposals/test_<slug>.py`) and confirm it FAILS,
lint both files (`uv run ruff check .` and `uv run ruff format`) because CI
lints the whole repo and a merged proposal must not turn main red for a
missing blank line, and open the PR with
`gh pr create --title "proposal: <candidate>" --body-file docs/proposals/<file>.md`.

**The gate.** `gh pr create` under the proposal gate is refused unless the
whole diff is exactly that one note and that one eval, the note carries its
`Candidate:` id and both sections, no other note already answers that id,
and the session's last pytest named the eval and failed. The refusal names
the half that failed; write it in the note and route around it.

**The issue is the owner's act.** Creating a GitHub issue is not a tool this
session has, so the `## Issue` section carries the row text and the owner's
merge is the signal to file it as a `ready` issue on the board. Say that in
the note.

**Record it** in Actions taken (the PR link and the candidate id) and in
Recurring patterns (the repeat, now proposed).

## Guardrails — non-negotiable

- **The auditor stays independent.** Never edit audit reports, never mute or
  reword findings, never touch `triage.toml` (owner triage is the owner's file),
  never write inside `_auditor/` except the `triage/` subfolder.
- **Engine invariants apply in full** (repo CLAUDE.md): eval-first, no direct
  main commits, agent brief + code same PR, no unpinned dependencies.
- **No money movement, no external sends, no QBO writes.** Anything
  approval-shaped routes to the owner, always.
- **Medical senders stay counted, never identified** — same rule as the mail
  fetch.
- One PR per work item; unrelated findings never share a branch.
- **A proposal PR never carries an implementation.** No source change, one
  failing eval, one design note. A proposal that implements the row is a
  fix PR wearing a costume, and the gate refuses it.
