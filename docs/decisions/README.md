# Decisions, one file each

Every load-bearing decision is ONE file in this folder,
`YYYY-MM-DD-<slug>.md`. Why: the table that came before was append-only, so any two
open PRs collided on its last line (three rebases on 2026-09-10 alone); one
file per decision cannot collide, and a decision reads as a page instead of
a 400-character table cell.

Shape (the eval `tests/unit/test_decision_files.py` pins it):

    # <title>
    Date: YYYY-MM-DD
    Type: One-way door | Two-way door | Refines <date>

    <the decision, the reasoning, what it supersedes or refines>

The runner does not read these files; they are the record. Cite the file
from the PR and from the code comment the decision governs.
