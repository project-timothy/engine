# The build lane picks its issue in code, and ships its schedule uninstalled
Date: 2026-09-12
Type: Two-way door

The headless build lane (phase 7 row 7.6, issue #215) is the audit-triage
shape pointed at the phase board: a zsh wrapper, a desk helper, a launchd
plist, a skill, and the skill harness's contracts. Two choices the row left
open are settled here.

**The pick is code, not model judgment.** `build-lane-desk.py pick` lists
the open issues labeled `ready` through `gh`, drops any that also carry
`blocked`, `in-pr`, or `one-way-door`, drops size-L rows (a design note
first, the owner's call), and orders the rest smallest first (S, then M or
unsized), then by plan row, then by number. The winner lands in the stage
as `issue.json` and `issue.md` before a session exists, and the session may
not swap it. Why: two fires over the same board must pick the same issue,
the owner must be able to predict the morning's PR from the labels alone,
and the labels become his throttle (remove `ready` to hold a row). A
session still stops with a SKIPPED note on what only it can see: an
acceptance that is not testable as written, a one-way door in the Door
text, a dependency whose PR has not merged, or Touches naming a forbidden
file.

**The plist ships uninstalled.** The owner's rule (phase 7 plan, "Order of
execution") says a new scheduled job waits for a coding day. So the plist
file is committed and pinned, the installer names it but does not bootstrap
it, and the test suite keeps it in a `STAGED_LABELS` set beside
`EXPECTED_LABELS`. The wrapper runs by hand from the dev tree today; the
coding-day PR is one line in the installer and one in the test. The skill
symlink (`~/.claude/skills/build`) does ride the installer now, because a
link is not a schedule and the hand run needs it.

**The PR body has its own contract.** The row asked the harness to assert
the PR body's sections; the harness checks any headed text, so the build
skill carries `pr-body-contract.toml` beside `contract.toml` rather than
growing the harness a second field. No `core/` change.

Refines nothing; the triage lane's headless contract (2026-09-01) is the
precedent.
