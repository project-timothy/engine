# Changelog

What changed between releases, newest first. Every section calls out two
things an operator must act on before upgrading: **tenant files** (a key in
`tenant.toml` or `vendors.toml` added, renamed, or removed) and **ledger
schema** (a migration; the engine applies it on the first run after the
upgrade, and there is no down-migration, so back up the ledger first).

`engine --version` and `auditor --version` print the version you run and the
commit (or, in a container, the image). A release is a `vX.Y.Z` tag on the
merge that sets `version` in `pyproject.toml`; that merge moves the
Unreleased section under the new number.

## Unreleased

**Tenant files.** New optional `[ap.provenance]` section in `tenant.toml`
(`internal_sender_domains`, `platform_sender_domains`), and an optional
`senders` list per vendor in `vendors.toml`. Both default to empty; nothing
existing is renamed.

**Tenant files.** New optional `[llm].document_tiers` (a list of tier
names) and a per-tier `zero_data_retention` (true, false, or unset). With
`document_tiers` set, a model call that carries a document (an invoice,
receipt, or scan sent as an attachment) goes only to a listed tier; any
other tier, fallbacks included, is refused before the call and recorded as
`documents_refused`. `engine doctor` lists the allowed tiers with their
retention and names any document job routed off the list. Unset keeps
today's behavior (#358).

**Act before upgrading: project codes are tenant config now.** The engine
no longer knows the `PYY_NNNN` project-number scheme; `[books.cost_object]`
in `tenant.toml` names it (`pattern`, `canonical`, optional `tag_pattern`).
With no pattern, no text reads as a project code: the hand-check card has
no project hint, receipt file names carry no project tag, and the
auditor's projects lens resolves only nicknames. A tenant that relied on
the old built-in scheme adds, before upgrading:

    [books.cost_object]
    pattern = "(?i)p\\s?n?\\s?(\\d{2})\\s?_?\\s?(\\d{4})"
    canonical = "P{0}_{1}"
    tag_pattern = "\\bPN?(?:(\\d{2})[_-])?(\\d{4})\\b"

Those are the three regexes the engine hard-coded before, and a replay of
the first tenant's ledger and receipt tree through old and new code found
no difference (#340).

**Ledger schema.** No change (still ledger migration 8).

- The agent lanes get their own container (issue #359): `Dockerfile.lane`
  (the lockfile's dependencies with the `[claude]` extra and the dev group,
  no engine code, uid 10001) and `compose.lane.yaml` (an internal network,
  a digest-pinned squid allowing GitHub and the Anthropic API only, and only
  the environment the file names). The product image is unchanged and stays
  SDK-free. Nothing schedules it yet; a tenant's wrapper runs it.
- `engine --version`, `auditor --version`, and a version line at the top of
  `engine doctor` (public #7).
- The engine refuses a ledger directory that has git history but no
  `ledger.sqlite3` (a restore that checked out nothing, or a deleted file)
  instead of starting a fresh, empty ledger over it; `engine run` exits 2 and
  names the re-clone. A first install, even with its backup remote already
  added, opens as before.
- The ledger can be restored from its remote, and a test proves it;
  `docs/recovery.md` walks a new box through it. The 23:00 job now packs the
  ledger repository (`git gc`) after the push, and `engine doctor` reports
  the ledger's commit count and size (public #13).
- AP records who mailed each invoice (`ap.provenance.recorded`, shadow only:
  a verdict, never a hold) and a new auditor lens, `provenance`. A new-vendor
  approval card can only be decided by a person at a terminal; the auditor
  raises CRITICAL on any other decision.
- `engine doctor --create-folders`, clearer configuration errors, a bounded
  read of the event log, and the Apache-2.0 `core/contracts/` package.
- `scripts/check.sh` runs every CI gate locally; CI lints its own workflows
  with zizmor (no persisted checkout credentials, no cache in the image job).
- CI: the suite passes as root, shellcheck runs on every shell script,
  accepted advisories carry a reason and a review date, and mypy checks the
  ledger, contracts, gateway, and adapters.
- The two QuickBooks push jobs moved out of `core/agents/ap/jobs.py` into
  `qbo_push.py` and `qbo_push_payments.py`, with no change in what they do;
  the payments run, complexity 28, is cut into steps of 9 or less (public #2).
- `engine doctor` fails when another tenant's credentials (its accounting
  token file, a configured mailbox, or a secrets file this user can decrypt)
  are reachable by the same OS user: one tenant per user or container.

## 0.1.0 (2026-10-04)

First public release.

**Ledger schema** at ledger migration 8. A ledger created by any earlier
build is brought forward on its first run:

- ledger migration 1: runs, events, and the approval queue.
- ledger migration 2: AP invoices and their status history.
- ledger migration 3: accounting-system bill and payment ids on AP invoices.
- ledger migration 4: expense reports and expense lines.
- ledger migration 5: the payment instrument on an expense report.
- ledger migration 6: job records (a file move is recorded before it happens).
- ledger migration 7: model-call telemetry (`llm_calls`).
- ledger migration 8: retry policy and attempt state on job records.
