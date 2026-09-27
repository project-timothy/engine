#!/usr/bin/env bash
# Sourced, never executed; the shebang is documentation. Every caller runs
# under bash (the container) or zsh (launchd on the Mac), so nothing here may
# use a construct only one of them has (row 7.20).
#
# One place decides which environment `uv run` syncs before a scheduled job
# (phase 7 row 7.21): one optional extra and one dependency group, asked for
# together on every call.
#
# `uv run` SYNCS before it runs: it makes the environment be exactly the set
# it was asked for and removes everything else. So the set is a property of
# the HOST, not of the job, and every scheduled call has to name the same one.
# It did not, and the 2026-09-17 Thursday sweep is what that cost: the 02:00
# and 08:00 scripts asked for `--extra claude` alone, uv dropped the host
# group each morning, and the sweep's browser helper died on
# `ModuleNotFoundError: No module named 'playwright'` with no note
# (docs/lessons.md, "One environment for the whole schedule", issue #280).
#
# The Mac installs the [claude] extra (the Claude Agent SDK, row 7.15) and the
# `host` group (Playwright, for the Thursday sweep). The container installs
# NEITHER: API keys only, no Claude Code, no Max seat, no browser (row 7.26's
# exit criterion). Asking for either there would send uv to resolve a package
# the locked sync deliberately left out, at 08:00, against a network the box
# may not have.
#
# Two knobs, same shape:
#   ENGINE_UV_EXTRA  the NAME of the one optional extra to sync
#   ENGINE_UV_GROUP  the NAME of the one dependency group to sync
# Unset (launchd sets neither) keeps the Mac on "claude" and "host"; empty
# means this host installed none and uv is asked for none. They are
# independent: a host may have one, both, or neither.
#
# Each names its value rather than carrying the flag string for a reason worth
# keeping: zsh does NOT word-split an unquoted parameter expansion, so
# `uv run $FLAGS` passes "--extra claude" as ONE argument under the shell
# launchd actually invokes, and the 08:00 run dies on an unknown option. An
# array would split in both shells, but the bash macOS ships is 3.2, where an
# EMPTY array's expansion is fatal under `set -u` (row 7.20, decision 2).
# Fixed arguments behind an `if` are the shape that behaves identically in
# both shells with no array and no eval.
#
# Usage (source it, then call):
#     . "$REPO/scripts/lib/uv-run.sh"
#     uv_run engine run "$TENANT" ap intake
if [ -n "${ENGINE_UV_EXTRA+set}" ]; then UV_EXTRA="$ENGINE_UV_EXTRA"; else UV_EXTRA="claude"; fi
if [ -n "${ENGINE_UV_GROUP+set}" ]; then UV_GROUP="$ENGINE_UV_GROUP"; else UV_GROUP="host"; fi

uv_run() {
  if [ -n "$UV_EXTRA" ] && [ -n "$UV_GROUP" ]; then
    "$UV" run --extra "$UV_EXTRA" --group "$UV_GROUP" "$@"
  elif [ -n "$UV_EXTRA" ]; then
    "$UV" run --extra "$UV_EXTRA" "$@"
  elif [ -n "$UV_GROUP" ]; then
    "$UV" run --group "$UV_GROUP" "$@"
  else
    "$UV" run "$@"
  fi
}
