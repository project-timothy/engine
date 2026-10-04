#!/usr/bin/env bash
# The container's first ten seconds (phase 7 row 7.21), in order:
#
#   1. make the data volume's shape
#   2. decrypt the tenant's secrets into the environment (row 7.22)
#   3. create the tenant on the volume if this is a first boot (row 7.19)
#   4. render the crontab from the tenant file          (engine schedule)
#   5. say what is still missing                        (engine doctor)
#   6. exec supercronic
#
# Step 2 is before step 5 on purpose: the doctor's whole job is to report what
# is missing, and if the secrets arrived after it, it would report every one of
# them missing on a box that has them.
#
# Nothing here prints a secret. `set -x` is deliberately absent: it would put
# every decrypted value into `docker logs`.
set -eu

TENANT="${ENGINE_TENANT:-demo}"
DATA="${ENGINE_DATA_ROOT:-/data}"
LOG_DIR="${LOG_DIR:-$DATA/logs}"
REPO="${ENGINE_REPO:-/app}"

export ENGINE_TENANTS_ROOT="${ENGINE_TENANTS_ROOT:-$DATA/tenants}"
export ENGINE_LEDGER_ROOT="${ENGINE_LEDGER_ROOT:-$DATA/ledger}"
export AUDITOR_STORE_ROOT="${AUDITOR_STORE_ROOT:-$DATA/auditor}"
export AUDITOR_TENANTS_DIR="${AUDITOR_TENANTS_DIR:-$ENGINE_TENANTS_ROOT}"

# The image runs as an unprivileged user (security review 2026-10-03). A volume
# created by an older image that ran as root is still root's: say the one fix
# and stop, rather than crashlooping on a permission error three steps in.
mkdir -p "$DATA" 2>/dev/null || true
if [ ! -w "$DATA" ]; then
  printf '[entrypoint] %s is not writable by %s (uid %s): it was made by an older image that ran as root.\n' \
    "$DATA" "$(id -un)" "$(id -u)" >&2
  printf '[entrypoint] fix it once:  docker compose run --rm --user root --entrypoint chown engine -R %s:%s %s\n' \
    "$(id -u)" "$(id -g)" "$DATA" >&2
  exit 1
fi

mkdir -p "$ENGINE_TENANTS_ROOT" "$ENGINE_LEDGER_ROOT" "$AUDITOR_STORE_ROOT" "$LOG_DIR"
cd "$REPO"

say() { printf '[entrypoint] %s\n' "$1"; }

# ---- 2. secrets -------------------------------------------------------------
# Two sources, both optional, neither ever logged.
#
# The encrypted one is row 7.22 and the one to use: sops decrypts
# tenants/<slug>/tenant.secrets.enc.yaml with an age identity that lives only
# on this box (SOPS_AGE_KEY_FILE, mode 0600, never in the image and never in a
# repository) and each NAME=value is exported here. The plaintext goes from
# sops into this shell's memory and into the environment; it is never written
# anywhere, which is why there is a process substitution below and not a file.
#
# The plain file is what row 7.21 shipped and stays for an operator who
# prefers compose `environment:` or an --env-file. The encrypted file wins
# where both exist, because it is the one that survives someone reading the
# volume.
#
# `resolve_secret` is not involved in any of this: it reads the environment,
# the way it does on every host that never heard of sops.
#
# Both files are DATA: host/secrets-env.sh parses each line and runs none of
# it, and refuses a name that would steer the process (PATH, LD_PRELOAD, the
# engine's own roots) rather than hand it a secret (security review
# 2026-10-03). The plain file used to be sourced as shell.
# shellcheck source=host/secrets-env.sh
. "$REPO/host/secrets-env.sh"
refused=""

SECRETS_FILE="${ENGINE_SECRETS_FILE:-$DATA/secrets.env}"
if [ -r "$SECRETS_FILE" ]; then
  say "reading $SECRETS_FILE (names only are ever printed)"
  while IFS= read -r line || [ -n "$line" ]; do
    export_secret_line "$line" plain
    [ "$SECRET_RESULT" = refused ] && refused="$refused $SECRET_NAME"
  done < "$SECRETS_FILE"
  unset line
fi

SECRETS_ENC="$ENGINE_TENANTS_ROOT/$TENANT/tenant.secrets.enc.yaml"
AGE_KEY="${SOPS_AGE_KEY_FILE:-}"
if [ -r "$SECRETS_ENC" ] && [ -n "$AGE_KEY" ] && [ -r "$AGE_KEY" ]; then
  export SOPS_AGE_KEY_FILE="$AGE_KEY"
  # The plaintext streams sops -> file descriptor -> this loop -> the
  # environment. It is never a file, and it is never even a shell variable
  # holding the whole of it. The loop runs in THIS shell (process
  # substitution, not a pipe) so the exports survive; sops' exit code rides
  # the stream on a last line, because a pipeline's status would belong to
  # the wrong process.
  names=""
  sops_rc=1
  while IFS= read -r line; do
    case "$line" in
      __sops_rc=*) sops_rc="${line#__sops_rc=}"; continue ;;
    esac
    export_secret_line "$line" sops
    case "$SECRET_RESULT" in
      exported) names="$names $SECRET_NAME" ;;
      refused) refused="$refused $SECRET_NAME" ;;
    esac
  done < <(sops --decrypt --output-type dotenv "$SECRETS_ENC" 2>/dev/null; printf '__sops_rc=%s\n' "$?")
  if [ "$sops_rc" = 0 ]; then
    say "decrypted $SECRETS_ENC, exported:$names"
  else
    # It failed, so there is no plaintext to leak: running it again with
    # stdout discarded is how the REASON reaches the log without the content.
    why="$(sops --decrypt "$SECRETS_ENC" 2>&1 >/dev/null | head -1)"
    say "SECRETS NOT LOADED: $SECRETS_ENC did not decrypt: $why"
    say "the box starts anyway; 'engine doctor $TENANT' is where the state of it is"
  fi
  unset line names sops_rc
elif [ -r "$SECRETS_ENC" ]; then
  say "SECRETS NOT LOADED: $SECRETS_ENC is here but no age identity is"
  say "(SOPS_AGE_KEY_FILE=${AGE_KEY:-unset}; see docs/credentials-checklist.md)"
fi

if [ -n "$refused" ]; then
  say "REFUSED from the secrets file (they steer the process, not secrets):$refused"
fi
unset refused SECRET_NAME SECRET_RESULT

# ---- 3. first boot ----------------------------------------------------------
# The tenant lives on the VOLUME, not in the image: the owner edits
# tenant.toml, and an image update must never overwrite it.
if [ ! -d "$ENGINE_TENANTS_ROOT/$TENANT" ]; then
  say "first boot: creating tenant $TENANT on the volume"
  uv run engine init "$TENANT" \
    --archetype "${ENGINE_ARCHETYPE:-A}" \
    --root "$ENGINE_TENANTS_ROOT" \
    --data-root "$REPO/data/$TENANT-data" \
    --no-audit
fi

# An argument means "run this instead of the scheduler": the ops door for
#     docker compose run --rm engine uv run engine queue list <tenant>
# with the same environment the scheduled jobs get, and no crontab rewritten.
if [ "$#" -gt 0 ]; then
  say "running: $*"
  exec "$@"
fi

# ---- 4. the crontab ---------------------------------------------------------
# Rendered at every boot, so a hand-edited copy never survives a restart and
# the schedule is always what the tenant file says. --every-minute is the
# smoke-test cycle: same commands, every minute instead of at 02:00.
CRONTAB="$DATA/crontab"
if [ -n "${ENGINE_SCHEDULE_EVERY_MINUTE:-}" ]; then
  say "rendering a FAST crontab (every minute): this is a smoke test, not a schedule"
  uv run engine schedule "$TENANT" --root "$ENGINE_TENANTS_ROOT" \
    --repo "$REPO" --log-dir "$LOG_DIR" --out "$CRONTAB" --every-minute
else
  uv run engine schedule "$TENANT" --root "$ENGINE_TENANTS_ROOT" \
    --repo "$REPO" --log-dir "$LOG_DIR" --out "$CRONTAB"
fi

# The schedule's timezone is the tenant's. supercronic reads it from the
# CRON_TZ line in the crontab; exporting it too makes the log timestamps of
# the jobs match the times in the file.
TZ="$(sed -n 's/^CRON_TZ=//p' "$CRONTAB" | head -1)"
if [ -n "$TZ" ]; then export TZ; else unset TZ; fi

# ---- 5. what is still missing ----------------------------------------------
# Reported, never fatal: a container that exits on a missing optional item is
# a crashloop, and most of the fixes are edits to the tenant file this box is
# serving. The exit code is still available to a person: `docker compose exec
# engine uv run engine doctor <tenant>`.
say "engine doctor $TENANT"
uv run engine doctor "$TENANT" --root "$ENGINE_TENANTS_ROOT" || \
  say "doctor found items above; the scheduler starts anyway (see docs/install.md)"

# ---- 6. the loop ------------------------------------------------------------
say "supercronic $CRONTAB"
exec supercronic "$CRONTAB"
