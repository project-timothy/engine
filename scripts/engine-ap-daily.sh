#!/usr/bin/env bash
# Daily AP run for the engine (replaces old Otto's otto-monitor, retired 2026-07-09).
# Rolling 10-day window: intake re-extracts only files that ARRIVED in the last
# 10 days (since filters by file mtime), so this stays cheap day to day. A longer
# outage is caught by a manual catch-up run with a wider --param since=YYYY-MM-DD.
#
# Runs under bash (the phase 7 container, row 7.20) AND under zsh: launchd
# still invokes `/bin/zsh <this file>`, so the shebang is not what executes it
# on the Mac. Nothing below may use a construct only one shell has — no zsh
# path modifiers, no zsh `print`, and no arrays (the Mac's bash is 3.2, whose
# empty-array expansion trips `set -u`).
set -u
# HOME from launchd (gui domain), a container's cron, or the shell; pinned so
# the state paths below derive from it (7.25: no host name in any script). The
# fallback asks the passwd database through ~user rather than assuming the
# macOS /Users layout.
if [ -z "${HOME:-}" ]; then HOME=$(eval printf '%s' "~$(id -un)"); fi
export HOME
# uv, off PATH. The plists pin PATH=/opt/homebrew/bin:... so this resolves to
# the binary that has always run here; a Linux host finds its own. UV= in the
# environment wins, and the Homebrew path stays as the last-resort fallback so
# a PATH-less context behaves exactly as it did before row 7.20.
if [ -z "${UV:-}" ]; then
  UV="$(command -v uv 2>/dev/null || true)"
  [ -n "$UV" ] || UV=/opt/homebrew/bin/uv
fi
# Every job below goes through uv_run (scripts/lib/uv-run.sh): `uv run` with
# this host's optional extra. uv syncs the environment before every run, and
# on this Mac that extra is what keeps the Claude Agent SDK in it, so the
# model-backed jobs never come up sdk_missing (row 7.15).
# The repo is wherever this script lives (issue #108): launchd points at the
# deploy clone's copy, so the checkout that runs is the checkout that was
# deployed — never whatever branch a dev session left behind. Resolved the
# same way run-preflight.sh resolves it, so the guard and the caller can never
# disagree about which repo they are in.
SELF="${BASH_SOURCE[0]:-$0}"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$REPO" || exit 2

# The tenant this host runs and its state home (issue #108: the ledger and the
# auditor store live at one path whichever checkout executes, so the deploy
# clone and interactive sessions share one ledger). The host names all three;
# the engine assumes none of them.
# shellcheck source=scripts/lib/require-env.sh
. "$REPO/scripts/lib/require-env.sh"
require_env ENGINE_TENANT ENGINE_LEDGER_ROOT AUDITOR_STORE_ROOT
TENANT="$ENGINE_TENANT"
export ENGINE_LEDGER_ROOT AUDITOR_STORE_ROOT

# Freshness guard: refuses stale/dirty/off-main checkouts (writes a marker the
# heartbeat lens reports); pulls when cleanly behind so merged fixes deploy.
# Off-box dead-man (scripts/lib/hc-ping.sh): start ping now, finish ping with
# the exit code at every exit path. No-op when HC_PING_BASE is unset.
source "$REPO/scripts/lib/hc-ping.sh"
# uv_run: `uv run` with this host's optional extra, or none (row 7.21).
source "$REPO/scripts/lib/uv-run.sh"
HC_SLUG="$TENANT-engine-ap-daily"
hc_ping "$HC_SLUG" start

if ! "$REPO/scripts/run-preflight.sh" engine-ap-daily; then
  hc_ping "$HC_SLUG" 78 "preflight refused or failed; the morning run did not run"
  exit 78
fi

# Ten days back in whichever `date` the host ships: BSD (macOS) takes -v, GNU
# (Linux) takes -d, and GNU rejects -v outright, which is what makes the probe
# a reliable discriminator. Same string either way.
days_ago() {
  if date -v-1d +%Y-%m-%d >/dev/null 2>&1; then
    date -v-"$1"d +%Y-%m-%d
  else
    date -d "$1 days ago" +%Y-%m-%d
  fi
}
SINCE=$(days_ago 10)
echo "=== engine-ap-daily $(date) (since=$SINCE) ==="

# The engine fetches its own mail (2026-07-17, replacing the unfiltered
# legacy feeders): filtered attachments land before intake classifies them.
uv_run engine run "$TENANT" mail fetch;                      rc_mail=$?
# AR (issue #282, 2026-09-17): a customer's remittance advice is body-only
# mail, so the attachment fetch above can never see it. This job reads the
# advice, records the payment once per payment number, and closes the loop
# against the Deposits the bank's own statement files carry.
uv_run engine run "$TENANT" ar remittance;                   rc_ar=$?
uv_run engine run "$TENANT" ap intake --param since="$SINCE"; rc_intake=$?
uv_run engine run "$TENANT" ap apply;                        rc_apply=$?
# Write side W1 (2026-07-17): queue/execute the bills batch. Writes happen
# only after the owner approves the card; unapproved days just re-park.
uv_run engine run "$TENANT" ap qbo-push;                     rc_qbopush=$?
# Write side W2 (phase 7 row 7.2, live since 7.3 on 2026-09-12): record a
# BillPayment for every scheduled check behind a card. A noop line when
# [qbo].payment_records is false.
uv_run engine run "$TENANT" ap qbo-push-payments;            rc_qbopushpay=$?
# Reconcile before workbook so payments cleared into the tenant's accounting
# book (which receives the bank feed) turn Paid in the same morning's sheet.
# Window comes from tenant [qbo].since_days. Activated 2026-07-16.
# 7.3/7.4: the tenant's whole [bank_csv].statement_dir rides along as
# --param statement_dir (the clearing signal for engine-written payments,
# which the feed click cannot show the API). Reconcile reads every statement
# file there each morning, PDFs and CSVs alike, and line identity makes
# re-reading the same months idempotent. An empty folder = the statement
# tier is skipped with one log line.
#
# The LISTING lives in the job now, never a zsh glob (2026-09-13): macOS TCC
# gates readdir on the Desktop tree per program identity, so the stat
# passed, the glob came back empty and silent, and reconcile read an
# environment failure as "no export this run". The engine reads and writes
# that tree all morning through this same `uv run` interpreter, and a folder
# it cannot look inside is an ap.reconcile.statement_unreadable anomaly on
# the run (and on the nightly audit), under a key that never replays.
#
# The --param rides an if/else rather than an array (row 7.20): bash 3.2, the
# bash macOS ships, treats an EMPTY array's expansion as an unbound variable
# under `set -u` and would kill the run at this line.
STATEMENT_DIR=$(uv_run python -c "from core.engine.config import load_tenant; print(load_tenant('$TENANT').bank_csv.statement_dir)" 2>/dev/null)
rc_statement=0
if [ -z "$STATEMENT_DIR" ]; then
  echo "statement tier: could not resolve [bank_csv].statement_dir from the tenant config"
  rc_statement=3
  uv_run engine run "$TENANT" ap reconcile;                  rc_reconcile=$?
else
  echo "statement tier: reading $STATEMENT_DIR"
  uv_run engine run "$TENANT" ap reconcile \
    --param "statement_dir=$STATEMENT_DIR";                                    rc_reconcile=$?
fi
# 7.4, the third card source: the weekly bank-feed sweep parks the feed rows
# its policy may not click into a dated note. This reads the NEWEST note in
# [qbo_sweep].note_dir and puts each parked row in the approval queue, so the
# answer lands where every other answer lands instead of waiting a week. It
# reads a file and parks questions: no click, no accounting write, no money.
# A tenant with no sweep (no note_dir) is one log line and rc 0, and a day
# with no new note replays the prior run by key and does nothing.
uv_run engine run "$TENANT" ap sweep-cards;                  rc_sweepcards=$?
uv_run engine run "$TENANT" ap workbook;                     rc_workbook=$?
uv_run engine run "$TENANT" timesheets intake;               rc_timesheets=$?
# The expenses agent (2026-08-04): receipts dropped in the shared drop folder are picked up and
# proposed every morning. Read-and-propose only — report builds and QBO
# writes stay owner-gated, so these two stages move no money and write no
# accounting records.
# Issue #112: classify the Taildrop Receipt Inbox (label-only) and execute
# any owner-approved filings/skips, BEFORE intake so a just-filed receipt
# flows into the same morning's intake+extract.
uv_run engine run "$TENANT" expenses inbox;                  rc_expinbox=$?
uv_run engine run "$TENANT" expenses intake;                 rc_expintake=$?
uv_run engine run "$TENANT" expenses extract;                rc_expextract=$?
# Janitor daily at the intake window (10d): anything older is inert to intake,
# so the landing top level stays at ~10 days of arrivals plus in-flight items
# (unfiled rows / pending approvals are never archived).
uv_run engine run "$TENANT" ap janitor --param days=10;      rc_janitor=$?
# The deadline calendar (2026-10-04): the dated obligations in the tenant's
# obligations.toml become reminders 90/30/7 days ahead (ledger events, once
# each) and a calendar file. Pure code, no send, no money; no file = off.
uv_run engine run "$TENANT" deadlines scan;                  rc_deadlines=$?
# Deadlines into the person's own calendar (2026-10-04): a plan of creates,
# updates and removals, written after an approved card or the tenant's
# one-time unattended grant. No calendar service detected = nothing to do.
uv_run engine run "$TENANT" deadlines calendar;              rc_calendar=$?
# The weekly brief (2026-10-04): Monday's first run writes the week's page
# from the read-only tools; later runs only work its send card. No send
# without an approved card or the tenant's unattended policy.
uv_run engine run "$TENANT" brief weekly;                    rc_brief=$?
# Project-registry drift as cards (#325): off until [projects].drift_cards.
uv_run engine run "$TENANT" projects drift;                  rc_projects=$?

summary="=== done: mail=$rc_mail ar=$rc_ar intake=$rc_intake apply=$rc_apply qbopush=$rc_qbopush qbopushpay=$rc_qbopushpay statement=$rc_statement reconcile=$rc_reconcile sweepcards=$rc_sweepcards workbook=$rc_workbook timesheets=$rc_timesheets expinbox=$rc_expinbox expintake=$rc_expintake expextract=$rc_expextract janitor=$rc_janitor deadlines=$rc_deadlines calendar=$rc_calendar brief=$rc_brief projects=$rc_projects ==="
echo "$summary"
# Nonzero if any stage failed, so launchd/logs (and the finish ping) surface it.
if [ $rc_mail -eq 0 ] && [ $rc_ar -eq 0 ] && [ $rc_intake -eq 0 ] && [ $rc_apply -eq 0 ] && [ $rc_qbopush -eq 0 ] && [ $rc_qbopushpay -eq 0 ] && [ $rc_statement -eq 0 ] && [ $rc_reconcile -eq 0 ] && [ $rc_sweepcards -eq 0 ] && [ $rc_workbook -eq 0 ] && [ $rc_timesheets -eq 0 ] && [ $rc_expinbox -eq 0 ] && [ $rc_expintake -eq 0 ] && [ $rc_expextract -eq 0 ] && [ $rc_janitor -eq 0 ] && [ $rc_deadlines -eq 0 ] && [ $rc_calendar -eq 0 ] && [ $rc_brief -eq 0 ] && [ $rc_projects -eq 0 ]; then
  rc=0
else
  rc=1
fi
hc_ping "$HC_SLUG" "$rc" "$summary"
exit $rc
