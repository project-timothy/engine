# Proposals

One file per proposal, `YYYY-MM-DD-<slug>.md`, written by the 06:00 triage
lane when lens 19 names an automation that has now repeated (phase 7 row
7.18, `skills/audit-triage/SKILL.md` "Proposal PRs: the eval budget").

Each file is also the body of the PR that carried it, so what the owner read
before merging is what the repo holds afterwards. The shape:

```
# proposal: <the candidate sentence>

Candidate: <lens 19's candidate id>

## Design
## Failing eval
## Issue
```

`Candidate:` is the id lens 19 computes from the source lens and condition
(`auditor/findings.py`, `candidate_id`). It is how a candidate is proposed
once and only once: the gate refuses a second proposal naming an id any file
here already carries.

**Merging a proposal is the decision, not the build.** The merged file comes
with one eval under `evals/proposals/` that FAILS by design; the board issue
in the `## Issue` section is filed as `ready` and the build lane implements
it, turning that eval green and moving it into `tests/` or the agent's own
`evals/` tree where CI runs it every time.

Closing a proposal PR unmerged is a decision too, and a final one: the
candidate is answered either way and the lane never raises it again.
