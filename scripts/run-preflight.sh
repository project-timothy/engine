#!/bin/bash
# Scheduled-run freshness guard (issue #108).
#
# Runs FIRST in every scheduled entry script, before any uv/python, because
# executing project code to check project freshness would run the possibly-
# stale code this guard exists to catch. bash + git only.
#
# Contract (tests/unit/test_run_preflight.py):
#   - the runtime checkout must be on main, clean, and fast-forwardable to
#     origin/main; the guard pulls when cleanly behind so merged fixes deploy
#     on the next scheduled run (the 2026-08-11 vendor re-nag ran a
#     checkout that lagged PR #105 by 14 hours);
#   - since the tenant cut (docs/decisions/2026-09-22-the-tenant-lives-in-its-
#     own-repository.md) the tenant is its own checkout, named by
#     ENGINE_TENANTS_ROOT, and the same rules hold for it, because a mute
#     merged in the tenant repository deploys the same way a fix does here;
#     its markers carry a "tenant-" reason. A tenants root that is not inside
#     a git checkout (an image, a plain folder) is left alone: nothing to
#     freshen;
#   - refusal = exit 1 + a refusal marker; the entry script must NOT run;
#   - an unreachable origin degrades instead of refusing: an offline night
#     runs the existing reviewed main rather than skipping the audit, and a
#     warning marker records the degradation;
#   - markers are JSON under $AUDITOR_STORE_ROOT/preflight/ (default:
#     <repo>/.auditor/preflight/); the auditor's heartbeat lens turns fresh
#     markers into checklist findings (CRITICAL for refusals, WARN for
#     degradations), so a refused run is loud the next time an audit runs.
#
# Usage: run-preflight.sh <job-name>

set -u

JOB="${1:?usage: run-preflight.sh <job-name>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
STORE_BASE="${AUDITOR_STORE_ROOT:-$REPO/.auditor}"
MARKER_DIR="$STORE_BASE/preflight"

# The checkout under guard right now, and the prefix its reasons carry: the
# engine's reasons are bare, the tenant's start with "tenant-".
GUARDED="$REPO"
PREFIX=""

note() { echo "[preflight:$JOB] $1"; }

repo_git() { git -C "$GUARDED" "$@"; }

sanitize() { # strip characters that would break the hand-built JSON
  printf '%s' "$1" | tr -d '"\\' | tr '\n' ' '
}

write_marker() { # kind reason detail
  local kind="$1" reason="$2" detail="$3"
  local ts fts branch head
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fts="$(date -u +%Y%m%dT%H%M%SZ)"
  branch="$(repo_git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  head="$(repo_git rev-parse HEAD 2>/dev/null || echo unknown)"
  mkdir -p "$MARKER_DIR"
  printf '{"job":"%s","kind":"%s","reason":"%s","detail":"%s","ts":"%s","branch":"%s","head":"%s"}\n' \
    "$(sanitize "$JOB")" "$kind" "$reason" "$(sanitize "$detail")" \
    "$ts" "$(sanitize "$branch")" "$head" \
    > "$MARKER_DIR/$kind-$(sanitize "$JOB")-$fts.json"
}

refuse() { # reason detail
  note "REFUSED ($PREFIX$1): $2"
  write_marker refusal "$PREFIX$1" "$2"
  exit 1
}

# guard_checkout <dir> <prefix>: the freshness rules against one checkout.
guard_checkout() {
  GUARDED="$1"
  PREFIX="$2"
  local branch
  branch="$(repo_git rev-parse --abbrev-ref HEAD 2>/dev/null)" \
    || refuse git-broken "cannot resolve HEAD in $GUARDED"

  [ "$branch" = "main" ] \
    || refuse branch-not-main "checkout $GUARDED is on '$branch'; a scheduled run only executes reviewed main"

  [ -z "$(repo_git status --porcelain)" ] \
    || refuse dirty-tree "checkout $GUARDED has uncommitted changes; refusing to run unreviewed code"

  # Low-speed abort so a hung fetch cannot stall a 02:00 job forever.
  if repo_git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch --quiet origin 2>/dev/null; then
    if ! repo_git merge --ff-only --quiet origin/main 2>/dev/null; then
      refuse diverged "local main in $GUARDED cannot fast-forward to origin/main; the checkout has unpushed or rewritten history"
    fi
    if [ "$(repo_git rev-parse HEAD)" != "$(repo_git rev-parse origin/main)" ]; then
      refuse head-mismatch "HEAD != origin/main in $GUARDED after fast-forward; checkout state is inconsistent"
    fi
    note "ok: ${PREFIX}HEAD=$(repo_git rev-parse --short HEAD) == origin/main"
  else
    write_marker warning "${PREFIX}fetch-failed" "could not reach origin for $GUARDED; running the existing main checkout"
    note "WARN (${PREFIX}fetch-failed): origin unreachable; proceeding on existing main HEAD=$(repo_git rev-parse --short HEAD)"
  fi
}

# A container image is a different kind of reviewed checkout (row 7.21): the
# image digest IS the review, there is no origin to fetch from, and pulling
# code into a running container is what an image deploy replaces. An image
# says so by setting ENGINE_IMAGE, and this branch applies ONLY where there is
# no repository to check, so the variable can never switch the guard off on a
# real checkout. No marker is written: every marker that is not a refusal
# becomes a nightly WARN in the heartbeat lens, and a container that filed one
# at 02:00, 08:00 and 23:00 would nag forever about being a container.
if ! repo_git rev-parse --git-dir >/dev/null 2>&1 && [ -n "${ENGINE_IMAGE:-}" ]; then
  note "ok: image ${ENGINE_IMAGE} (no checkout here; the image is the reviewed artifact)"
  exit 0
fi

guard_checkout "$REPO" ""

if [ -n "${ENGINE_TENANTS_ROOT:-}" ]; then
  tenant_top="$(git -C "$ENGINE_TENANTS_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$tenant_top" ] && [ "$tenant_top" != "$REPO" ]; then
    guard_checkout "$tenant_top" "tenant-"
  fi
fi

exit 0
