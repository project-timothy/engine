# The triage lane on the runner: the note is the result, the gate is not optional
Date: 2026-09-17
Type: Two-way door

Phase 7 row 7.17 moved the 06:00 audit triage off the bare `claude` binary
and onto `core/llm/runner.py` through a new CLI, `engine runner run`. Four
choices the row did not spell out, decided here because each one is a thing a
later lane will copy.

## The note is the session's final message, and the CLI writes it

The old session wrote the note itself to `AUDIT_TRIAGE_NOTE_STAGE`. Under the
runner the note stage is outside the file root, so a write there would be
refused by the gate, and the runner already has a note: `result.note`, which
is the session's last message. So the CLI takes `--param note=<path>` and
writes it.

It writes it **only when the status is FINISHED**. A budget stop yields FAILED
with the last progress text as the note, and the wrappers install whatever
note they find: a partial note that looks finished is the one thing a morning
reader must never get. The progress still reaches the log and the transcript,
so nothing is lost, and a missing note keeps meaning exactly what it has
always meant.

## `gh` always carries `eval_first`

The eval-first rule used to be prose in the skill: write the failing test
first, never merge. Prose is advice. The gate is code: `gh pr create` is
refused unless the worktree carries a new or changed file under `tests/` or
an `evals/` directory AND a `pytest` run in the session passed after the last
write. The CLI admits that gate on **every** `gh` tool it builds, with no
parameter to turn it off, because a knob for it is a knob somebody sets to
false at 06:00 on a morning nobody is watching.

## The wrapper makes the worktree, and sweeps it when it is empty

`git worktree` is not a runner subcommand, and `git switch main` is refused
by the gate, so a session working directly in the dev tree could not return
it to main and the next night would skip on a busy tree. The wrapper cuts a
worktree off `origin/main` under the dev tree's gitignored
`.claude/worktrees/`, hands it to the runner as the root, and afterwards
removes it (and its branch) when it holds no commit and no change. A checkout
a night, each growing its own venv the first time a session runs pytest, is a
disk leak on a box where disk is a live constraint; a checkout that carries a
commit is work, and work is never thrown away.

## Which runner runs comes from the policy, not the command line

`runner_kind_for` reads the tier's adapter: the Claude Agent SDK tier gets the
SDK runner, every other adapter gets the in-repo loop, because every other
adapter is a `complete()` adapter and that is what the loop runs on. A product
host on API keys therefore runs this lane with the same wrapper and the same
command line. `--adapter` overrides it for a hand run or an offline replay.

Refines `docs/decisions/2026-09-12-runner-contract.md`, which is the one-way
door this builds on; nothing here changes that contract.
