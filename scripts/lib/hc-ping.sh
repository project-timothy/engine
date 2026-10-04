#!/usr/bin/env bash
# Sourced, never executed; the shebang is documentation. Every caller runs
# under bash (the phase 7 container) or zsh (launchd on the Mac), so nothing
# here may use a construct only one of them has (row 7.20).
#
# Dead-man pings to healthchecks.io (off-box watchdog, 2026-08-18).
#
# Every watchdog this back office had before today ran ON the mini (auditor,
# heartbeat lens, host lens, the 8 AM brief). A box that is dark, or parked at
# the FileVault unlock screen after a power cut, is therefore silent. This
# library gives each scheduled job a start ping and a finish ping carrying its
# exit code; healthchecks.io emails when a job is late or reports non-zero.
#
# Usage (source it, then call):
#     source "$REPO/scripts/lib/hc-ping.sh"
#     hc_ping acme-auditor-nightly start
#     ... run ...
#     hc_ping acme-auditor-nightly "$rc" "=== done: auditor=$rc ==="
#
#   hc_ping <slug> [start|<exit-code>] [message]
#     slug       the check's slug in the healthchecks project
#     start      marks the run as started (measures duration; a start with no
#                finish inside the grace period alerts)
#     <n>        finish with exit code n; 0 = success, anything else = fail
#     message    optional body shown in the healthchecks event log
#
# Configuration: HC_PING_BASE, e.g. https://hc-ping.com/<ping-key>. Read from
# the environment, else from the host's env file: ENGINE_HOST_ENV (the host
# names it), else ~/.config/engine/host.env (mode 600, NOT in any repo; keep
# the ping key in a password manager too). When unset the function is a
# silent no-op, so CI, dev sessions, and a machine without the env file never
# ping. Never fails the caller: a monitoring hiccup must not fail a job.
ENGINE_HOST_ENV="${ENGINE_HOST_ENV:-$HOME/.config/engine/host.env}"

hc_ping() {
  local slug="${1:?hc_ping: slug required}"
  local outcome="${2:-0}"
  local message="${3:-}"
  if [[ -z "${HC_PING_BASE:-}" && -r "$ENGINE_HOST_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$ENGINE_HOST_ENV"
  fi
  [[ -n "${HC_PING_BASE:-}" ]] || return 0
  local url="${HC_PING_BASE%/}/$slug"
  case "$outcome" in
    start) url="$url/start" ;;
    0) ;;
    *) url="$url/$outcome" ;;
  esac
  local curl_bin
  curl_bin="$(command -v curl)" || return 0
  local -a args
  args=(-fsS -m 10 --retry 3 -o /dev/null -A "engine/hc-ping")
  if [[ -n "$message" ]]; then
    args+=(--data-raw "$message")
  fi
  "$curl_bin" "${args[@]}" "$url" 2>/dev/null \
    || printf '%s\n' "hc-ping: could not reach the ping service for $slug ($outcome; monitoring only; job unaffected)" >&2
  return 0
}
