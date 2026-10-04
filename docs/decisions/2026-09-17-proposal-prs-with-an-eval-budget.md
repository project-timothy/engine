# Proposal PRs: the merged eval is the decision, and it is red on purpose
Date: 2026-09-17
Type: Two-way door

Phase 7 row 7.18. Lens 19 already names the automation that would retire a
repeat; row 7.18 turns a named candidate into a PROPOSAL PR: a design note
and one failing eval, no implementation. Five choices the row did not spell
out, decided here because each one is a thing a later lane will copy.

## The failing eval lives in a top-level `evals/` tree, outside `testpaths`

A proposal's eval fails by design. That is the whole point: the owner merges
a red assertion as a decision, before anything is built. But every existing
eval tree (`tests/`, `core/**/evals/`, `auditor/evals/`) is inside pytest's
`testpaths`, so merging a red eval into any of them turns CI red on main and
the next lane cannot tell a decision from a regression.

`pyproject.toml` names `testpaths = ["tests", "core", "auditor"]`, so a
top-level `evals/` tree is collected by nobody unless a path names it. That
is where proposals put their eval, and `evals/README.md` says what happens
next: the build lane makes it pass and MOVES it into a tree CI watches. An
eval that goes green and stays out of CI protects nothing.

The alternative was `xfail(strict=True)` inside the normal trees. It fails
the same job differently: a strict xfail reports green, so the board issue
looks done before it starts, and the marker has to be removed by the very
build the marker was supposed to demand.

## The design note IS the PR body

`gh pr create --body-file docs/proposals/<file>.md`, enforced at the gate. A
proposal read before merging and a proposal read six months later are then
the same bytes. The alternative (a body written into a scratch file) needs a
second file in the worktree, which the gate would have to admit, which is
exactly the hole "no source change" is there to close.

## The candidate id is the once-only key, and the repo is where it lives

The session's only durable surface is a commit: the allowlist roots every
tool in the night's worktree, and nothing there can reach the auditor's
store or any state outside it. So the record that a candidate was proposed
is the proposal itself. `Candidate: <id>` sits on its own line in the note,
and the gate refuses a proposal whose id any other note under
`docs/proposals/` already carries.

The id names the AUTOMATION, not the finding: `candidate_id(lens,
condition)` over the SOURCE lens and condition, so `long-lived`, `class`,
and `recurring` on one repeat agree on one id and one proposal answers all
three. Until the first proposal merges its note is only on a branch, so the
unmerged second night is caught by prose (the previous triage note is in the
bundle and the skill says a candidate is proposed once) rather than by the
gate. The cost of that gap is one duplicate PR the owner closes.

## The board issue waits for the merge

`gh issue create` is not a runner subcommand, so the session cannot file the
`ready` issue even if it wanted to: `GhSubcommand` is a closed Literal. That
settles a question the row left open. The `## Issue` section of the note
carries the row in the lane-issue shape, and the owner's merge is the signal
to file it. Filing it automatically from a merged proposal is a later row,
and it belongs on the build lane's side of the fence, not the triage's.

## The proposal gate rides beside `eval_first`, never instead of it

One session, two kinds of PR, one gate list: `["eval_first", "proposal"]`.
The gate picks by the SHAPE of the diff, not by a flag the model sets. A
diff that touches `docs/proposals/` is judged as a proposal (one note, one
new eval under `evals/`, nothing else, and a pytest that named that eval and
FAILED); anything else is judged by `eval_first` with its green suite. So a
proposal that also carries an implementation cannot slip through either
door, and no lane can be configured to open a PR with no eval at all.

The budget is one proposal a night, and only on a night that opened no fix
PR: a fix in the worktree is a source change, which makes the diff not
proposal-shaped. The skill says it in prose and the gate enforces it in
Python, from opposite ends.
