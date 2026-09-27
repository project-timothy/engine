#!/usr/bin/env bash
# Off-box dead-man heartbeat (phase 7 row 7.21): "this host is alive".
#
# The Mac has run this every 30 minutes since 2026-08-18 as inline zsh inside
# the host-heartbeat launchd plist. A container has no plists, so the same
# ping becomes a crontab line and the body moves here. The Mac's plist is NOT
# changed by this file existing: launchd keeps running its own inline copy,
# and this script is what the container's crontab names.
#
# Deliberately shell and curl only: no uv, no engine code, no ledger, no
# freshness guard. The dead man must not depend on the thing it watches. A
# host with no HC_PING_BASE pings nothing and exits 0, so a tenant that never
# wired an off-box watchdog is silent rather than noisy.
#
# Runs under bash and zsh alike (row 7.20): no zsh path modifiers, no zsh
# print, no arrays.
set -u
# HOME from the scheduler or the shell; the ping library reads its config file
# under it. The fallback asks the passwd database through ~user.
if [ -z "${HOME:-}" ]; then HOME=$(eval printf '%s' "~$(id -un)"); fi
export HOME
SELF="${BASH_SOURCE[0]:-$0}"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
# The tenant this host runs, named by the host; its slug is half of the
# check's name.
# shellcheck source=scripts/lib/require-env.sh
. "$REPO/scripts/lib/require-env.sh"
require_env ENGINE_TENANT
TENANT="$ENGINE_TENANT"

# shellcheck source=scripts/lib/hc-ping.sh
. "$REPO/scripts/lib/hc-ping.sh"
# A finish ping with exit code 0 and no start ping: the heartbeat measures
# nothing, it only proves the box and its scheduler are up. healthchecks.io
# emails when the pings stop.
hc_ping "$TENANT-host-heartbeat" 0
exit 0
