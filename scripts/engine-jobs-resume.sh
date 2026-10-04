#!/usr/bin/env bash
# Due job retries, every 15 minutes (phase 7 row 7.21; the policy and the CLI
# are row 7.23, docs/retries.md).
#
# `engine jobs resume <tenant>` executes the retries whose next attempt is
# due and prints one line when nothing is. No production job declares a retry
# policy yet, so this is a noop until one does.
#
# No freshness guard here, deliberately: the guard writes a marker on every
# refusal, the heartbeat lens turns fresh markers into findings, and a job
# that runs 96 times a day would bury the nightly checklist in copies of one
# fact. This runs the same checkout the morning's guarded run already vetted.
#
# Runs under bash and zsh alike (row 7.20).
set -u
if [ -z "${HOME:-}" ]; then HOME=$(eval printf '%s' "~$(id -un)"); fi
export HOME
# uv, off PATH; UV= in the environment wins, Homebrew is the last resort.
if [ -z "${UV:-}" ]; then
  UV="$(command -v uv 2>/dev/null || true)"
  [ -n "$UV" ] || UV=/opt/homebrew/bin/uv
fi
SELF="${BASH_SOURCE[0]:-$0}"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$REPO" || exit 2

# The tenant and its state home (issue #108), named by the host.
# shellcheck source=scripts/lib/require-env.sh
. "$REPO/scripts/lib/require-env.sh"
require_env ENGINE_TENANT ENGINE_LEDGER_ROOT AUDITOR_STORE_ROOT
TENANT="$ENGINE_TENANT"
export ENGINE_LEDGER_ROOT AUDITOR_STORE_ROOT

# uv_run: `uv run` with this host's optional extra, or none (row 7.21).
# shellcheck source=scripts/lib/uv-run.sh
. "$REPO/scripts/lib/uv-run.sh"
uv_run engine jobs resume "$TENANT"
exit $?
