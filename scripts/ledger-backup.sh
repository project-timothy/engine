#!/usr/bin/env bash
# Nightly ledger push (23:00): the AP ledger's own git repo goes to the private
# remote the tenant configures, the ledger's off-machine copy
# (24 h RPO). Until 2026-08-18 the plist ran `git push` directly; this wrapper
# exists so the push carries a dead-man ping (scripts/lib/hc-ping.sh) — a
# push that stops happening used to be invisible until someone looked.
# No engine code runs here, so no freshness guard: the repo path is only the
# home of the ping library.
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
# git, NOT off PATH: /usr/bin/git is the binary this job has always run, and
# the launchd PATH puts Homebrew's copy first. Preferring the absolute path
# keeps the 23:00 push on the same git it has been pushing with; a host
# without it (a slim container) falls back to whatever PATH names.
if [ -z "${GIT:-}" ]; then
  if [ -x /usr/bin/git ]; then GIT=/usr/bin/git; else GIT="$(command -v git)"; fi
fi
SELF="${BASH_SOURCE[0]:-$0}"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
# The tenant whose ledger is pushed, and where its ledger lives: the host
# names both, the engine assumes neither.
# shellcheck source=scripts/lib/require-env.sh
. "$REPO/scripts/lib/require-env.sh"
require_env ENGINE_TENANT ENGINE_LEDGER_ROOT
TENANT="$ENGINE_TENANT"
LEDGER_REPO="$ENGINE_LEDGER_ROOT/$TENANT"

source "$REPO/scripts/lib/hc-ping.sh"
HC_SLUG="$TENANT-ledger-backup"
hc_ping "$HC_SLUG" start

echo "=== ledger-backup $(date) ==="
out=$("$GIT" -C "$LEDGER_REPO" push origin main 2>&1)
rc=$?
printf '%s\n' "$out"
summary="=== done: ledger push rc=$rc ($(printf '%s\n' "$out" | tail -1)) ==="
echo "$summary"
hc_ping "$HC_SLUG" "$rc" "$summary"
exit $rc
