# The decision log becomes one file per decision
Date: 2026-09-10
Type: Two-way door

`docs/decision-log.md` was append-only: every PR added a row at the end, so
any two PRs open at the same time conflicted on the same last line. On
2026-09-10 that produced three rebases in one evening (#198 against #197,
#199 against #198, #200 against #198) for changes that had nothing to do
with each other. The recurrence lens built the same day would have named
this class on its own once it had the reopen history.

From now on a decision is one file in `docs/decisions/`, named
`YYYY-MM-DD-<slug>.md`, with a title line, a Date line, and a Type line
(the same three columns the table had), then the reasoning as prose. The
table in `decision-log.md` is frozen at 55 rows and an eval refuses new
rows there. Two PRs deciding two things touch two files and never collide;
two PRs deciding the same thing on the same day is the collision that
SHOULD stop a merge.
