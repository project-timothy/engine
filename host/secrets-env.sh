# shellcheck shell=bash
# One line of a secrets file into the environment, as data (security review
# 2026-10-03, #390). Sourced by host/entrypoint.sh for both secret sources:
#
#   export_secret_line "<line>" plain   /data/secrets.env: an optional leading
#                                       `export `, one pair of surrounding
#                                       quotes stripped, nothing expanded
#   export_secret_line "<line>" sops    `sops --output-type dotenv`: verbatim
#
# Nothing in the line is ever run. A name that would steer the process rather
# than hand it a secret (PATH, LD_PRELOAD, GIT_SSH_COMMAND, the engine's own
# roots, ...) is refused. Sets SECRET_RESULT to exported, skipped or refused,
# and SECRET_NAME; never returns non-zero, so a `set -e` caller is safe.

secret_name_ok() {
  case "$1" in
    '' | [0-9]* | *[!A-Za-z0-9_]*) return 1 ;;
    PATH | HOME | SHELL | IFS | ENV | BASH_ENV | PS4 | PROMPT_COMMAND | CDPATH | TZ) return 1 ;;
    USER | LOGNAME | PWD | OLDPWD | TMPDIR | LANG | LC_*) return 1 ;;
    LD_* | DYLD_* | PYTHON* | UV_* | GIT_* | SOPS_* | BASH_FUNC_* | SSL_* | REQUESTS_CA_*) return 1 ;;
    ENGINE_*_ROOT | ENGINE_REPO | ENGINE_TENANT | ENGINE_IMAGE | ENGINE_SECRETS_FILE) return 1 ;;
    AUDITOR_* | LOG_DIR) return 1 ;;
  esac
  return 0
}

export_secret_line() {
  local line="$1" mode="${2:-sops}" value
  SECRET_RESULT=skipped
  SECRET_NAME=""
  if [ "$mode" = plain ]; then
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#export }"
  fi
  case "$line" in
    '' | '#'*) return 0 ;;
    *=*) ;;
    *) return 0 ;;
  esac
  SECRET_NAME="${line%%=*}"
  value="${line#*=}"
  if ! secret_name_ok "$SECRET_NAME"; then
    SECRET_RESULT=refused
    return 0
  fi
  if [ "$mode" = plain ] && [ "${#value}" -ge 2 ]; then
    case "$value" in
      \"*\") value="${value:1:${#value}-2}" ;;
      \'*\') value="${value:1:${#value}-2}" ;;
    esac
  fi
  export "$SECRET_NAME=$value"
  SECRET_RESULT=exported
  return 0
}
