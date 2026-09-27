# Triage 2026-09-17 (runner)

Report audit-2026-09-17.md read from the context bundle; one CRITICAL finding, one PR attempted and refused.

## New findings

- Lens 3 (materiality): one outlier of $9,400.00 against a vendor median of $310.00.
- Resolved since the last report: nothing.

## Root cause

- The lens divides by the vendor's median without guarding an empty history, so a first-ever invoice reads as an outlier. The bug is one line; the fix is one line.

## Actions taken

- None. The fix was written but the PR was REFUSED by the runner's `eval_first` gate: no new or changed test file existed in the worktree, so `gh pr create` never ran. That is the rule working, not a failure: an eval-first lane does not open a PR that carries only an implementation.
- The worktree is on its branch with the diagnosis and nothing committed.

## Recommended actions for the owner

- Nothing to merge tonight; the next session writes the failing eval first and opens the PR then. Recommendation: leave it to the lane rather than hand-fixing the lens.
- [NEEDS REVIEW] whether the guard belongs in the lens or in the shared statistics helper both lenses call.

## Recurring patterns

- Empty-history division has now surfaced twice; the automation that retires it is a shared median helper with the guard in one place.

## Watch list

- The 09-18 report should still carry the outlier; its disappearance without a merged fix would mean the data moved, not the bug.
