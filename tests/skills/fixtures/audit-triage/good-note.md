# Triage 2026-09-11 (headless)

Report audit-2026-09-11.md read from the stage; the previous note closed two items.

## New findings

- Lens 7 (reconcile): one unknown clearing, check 2210 for $1,240.00, payee text "NORTHWIND TOOLING".
- Lens 12 (registry freshness): the project registry snapshot is 9 days old (threshold 7).
- Resolved since the last report: the orphan project code on ledger row 91 (fixed by PR #412, merged by the owner).

## Root cause

- Check 2210: a hand-paid vendor check with no AP row. The ledger never saw an invoice for Northwind Tooling; the bank feed cleared it on 09-09. Mechanism confirmed by reading the ledger read-only: no row with check_ref 2210.
- Registry freshness: the Sunday registry sync job has not run since 09-01; its launchd plist is loaded but the source workbook moved. [NEEDS REVIEW] on the move date.

## Actions taken

- None that change state. The registry sync is a UI-side act, so it is recommended below rather than executed.
- Verified read-only that the ledger has no row for check 2210 (sqlite read, no writes).

## Recommended actions for the owner

- Tell the engine what check 2210 was: `uv run engine queue approve <card> --param vendor="Northwind Tooling"` after the invoice is dropped in the AP inbox, or mute it in triage.toml if it was a personal draw. Recommendation: it is a vendor check (the payee text is a business), so drop the invoice.
- Point the registry sync at the workbook's new folder, then re-run it once; the freshness lens clears the next night.

## Recurring patterns

- Hand-paid vendor checks with no AP row surfaced three of the last five weeks; the automation that retires it is a "check with no row" card that asks for the invoice.
- Registry freshness has tripped twice since 09-01; porting the sync into the engine retires the Sunday job.

## Watch list

- The 09-12 report should show check 2210 acknowledged or carded.
- Registry freshness INFO expected until the sync re-runs.
