"""The auditor (Vince-2): an independent application checking on the workflows.

Design: docs/auditor-design.md. The one principle everything follows from:
a checker that shares the worker's code shares the worker's bugs, so this
package imports NOTHING from ``core/`` (enforced by
``auditor.evals.independence_lint``, wired into CI). It recomputes answers
from ground truths — the ledger SQLite read-only, the event log, the
delivered workbook, the mailbox, the accounting system — with its own
queries and clients, and diffs those derivations against what the engine
produced.

It writes nowhere the engine writes: its own store lives under
``.auditor/<tenant>/`` and its only delivery surface is the nightly
checklist report in the tenant's report folder.
"""
