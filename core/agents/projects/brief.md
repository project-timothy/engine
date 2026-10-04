# Projects agent brief

## Mission

Give every project-registry drift item a home: one approval card the owner
answers once, instead of a bullet in a file whose only reader reports it
(#325, the 2026-09-19 proposal; lens 17 had carried the same INFO for 25
nights when this shipped).

## What it does

- **drift** reads the newest `project-registry-drift-*.md` beside
  `[projects].registry_path` (written by the tenant's own sync), classifies
  each bullet into one kind (`qbo-project-missing`, `folder-missing`,
  `one-source`, `new-since-sync`; anything else is `unclassified`, reported
  and never acted on) and parks one `projects.registry_drift` card per item,
  keyed on (project, kind), never on the filename.
- On approval: a decision-only kind says the registry line to add (the
  engine never writes the registry file); `folder-missing` creates the
  folder under `[projects].folder_root`, create only; `qbo-project-missing`
  names the owner's UI act until the API create (readback-verified, in
  `drift.py`) passes its live probe on a real missing project.
- A rejected card is a permanent answer; a decided item never cards again.
- Off until `[projects].drift_cards = true`.

## Boundaries (never)

- Never writes the registry file or anything under the auditor's trees.
- Never deletes, moves, or renames a folder; never touches money, a
  transaction, or an account.
- Never acts without an approved card, and never in shadow.
