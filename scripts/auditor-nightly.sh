#!/usr/bin/env bash
# Nightly independent audit (docs/auditor-design.md). Runs at 02:00, six
# hours from the engine's 08:00, so the QBO token-file lock never contends
# in practice (and is safe under the lock regardless). The auditor reads
# ground truths with its own code, reconciles its running checklist, and
# writes the day's report — its only delivery surface. Exit code surfaces
# through launchd logs; a run that dies is caught by the next night's
# heartbeat lens reading the auditor's own run table.
#
# Runs under bash (the phase 7 container, row 7.20) AND under zsh: launchd
# still invokes `/bin/zsh <this file>`, so the shebang is not what executes it
# on the Mac. Nothing below may use a construct only one shell has.
set -u
# HOME from launchd (gui domain), a container's cron, or the shell; pinned so
# the state paths below derive from it (7.25: no host name in any script). The
# fallback asks the passwd database through ~user rather than assuming the
# macOS /Users layout.
if [ -z "${HOME:-}" ]; then HOME=$(eval printf '%s' "~$(id -un)"); fi
export HOME
# uv, off PATH. The plists pin PATH=/opt/homebrew/bin:... so this resolves to
# the binary that has always run here; a Linux host finds its own. UV= in the
# environment wins, and the Homebrew path stays as the last-resort fallback.
if [ -z "${UV:-}" ]; then
  UV="$(command -v uv 2>/dev/null || true)"
  [ -n "$UV" ] || UV=/opt/homebrew/bin/uv
fi
# The run goes through uv_run (scripts/lib/uv-run.sh): `uv run` with this
# host's optional extra. On this Mac that extra is what keeps the Claude Agent
# SDK in the environment for the advisory drafter (row 7.15).
# The repo is wherever this script lives (issue #108): launchd points at the
# deploy clone's copy, so the checkout that runs is the checkout that was
# deployed — never whatever branch a dev session left behind. Resolved the
# same way run-preflight.sh resolves it, so the guard and the caller can never
# disagree about which repo they are in.
SELF="${BASH_SOURCE[0]:-$0}"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$REPO" || exit 2

# The tenant this host audits and its state home (issue #108). The host names
# all three; the engine assumes none of them.
# shellcheck source=scripts/lib/require-env.sh
. "$REPO/scripts/lib/require-env.sh"
require_env ENGINE_TENANT ENGINE_LEDGER_ROOT AUDITOR_STORE_ROOT
TENANT="$ENGINE_TENANT"
export ENGINE_LEDGER_ROOT AUDITOR_STORE_ROOT

# Off-box dead-man (scripts/lib/hc-ping.sh): start ping now, finish ping with
# the exit code at every exit path. No-op when HC_PING_BASE is unset.
source "$REPO/scripts/lib/hc-ping.sh"
# uv_run: `uv run` with this host's optional extra, or none (row 7.21).
source "$REPO/scripts/lib/uv-run.sh"
HC_SLUG="$TENANT-auditor-nightly"
hc_ping "$HC_SLUG" start

# Freshness guard: refuses stale/dirty/off-main checkouts (writes a marker the
# heartbeat lens reports); pulls when cleanly behind so merged fixes deploy.
if ! "$REPO/scripts/run-preflight.sh" auditor-nightly; then
  hc_ping "$HC_SLUG" 78 "preflight refused or failed; the audit did not run"
  exit 78
fi

echo "=== auditor-nightly $(date) ==="
uv_run auditor run "$TENANT"
rc=$?
summary="=== done: auditor=$rc ==="
echo "$summary"
hc_ping "$HC_SLUG" "$rc" "$summary"
exit $rc
