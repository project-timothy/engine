# shellcheck shell=bash
# require_env NAME... : stop, exit 78 (EX_CONFIG), naming the first variable
# the host did not set.
#
# An engine checkout carries no tenant of its own (extraction gate 2), so the
# scheduled entry scripts never guess which tenant, ledger or auditor store
# they serve: the host names them (the launchd plist, the rendered crontab,
# or the container's environment). A guessed default is how a checkout
# quietly runs the wrong business's books; a refusal names the fix. Sourced
# by bash and by zsh, so it uses nothing either one lacks.
require_env() {
  for _require_name in "$@"; do
    eval "_require_value=\${${_require_name}:-}"
    if [ -z "$_require_value" ]; then
      printf '%s: %s is not set; the host names it (the plist, the crontab or the container)\n' \
        "${0##*/}" "$_require_name" >&2
      exit 78
    fi
  done
}
