#!/usr/bin/env bash
# Every gate CI's `check` job runs, here, before the push (2026-10-07).
#
#   scripts/check.sh
#
# Runs them all and reports every failure, not the first one, so one pass
# says everything a PR still owes. Exit 0 only when every gate passed. The
# lines below are CI's own commands, verbatim: tests/unit/test_check_script.py
# fails when ci.yml gains a gate this file does not run.
#
# With ENGINE_LINT_TOKENS naming a tenant's private token list, the boundary
# lint also runs with it (and the wide scan reports, as in CI). CI's other two
# jobs, the suite without the SDK and the container cycle, run only in CI.
set -u
cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." || exit 2
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failed=""
gate() {
  local name="$1"
  shift
  printf '\n== %s\n' "$name"
  if "$@"; then
    printf -- '-- ok: %s\n' "$name"
  else
    printf -- '-- FAILED: %s\n' "$name"
    failed="$failed
  $name"
  fi
}

advisories() {
  uv export --locked --no-dev --all-extras --no-hashes --no-emit-project \
    --format requirements-txt > "$TMP/requirements.txt" || return 1
  local ignores
  ignores="$(uv run python -m core.evals.advisory_exceptions)" || return 1
  # shellcheck disable=SC2086  # one flag pair per accepted exception
  uvx --from pip-audit==2.10.1 pip-audit -r "$TMP/requirements.txt" \
    --no-deps --disable-pip --progress-spinner off $ignores
}

gate "lock is current and pinned" uv lock --check
gate "sync" uv sync --locked --extra claude
gate "advisory scan" advisories
gate "mypy" uvx --from mypy==2.4.0 mypy --python-executable .venv/bin/python
gate "ruff check" uv run ruff check .
gate "ruff format" uv run ruff format --check .
gate "bleed-through lint" env -u ENGINE_LINT_TOKENS uv run python -m core.evals.bleedthrough_lint
gate "independence lint" uv run python -m auditor.evals.independence_lint
gate "shellcheck" sh -c "git ls-files -z '*.sh' | xargs -0 shellcheck -x --severity=style"
gate "zizmor" uvx --from zizmor==1.30.1 zizmor --offline .github/workflows
gate "vulture" uv run vulture core auditor tenants vulture_whitelist.py --min-confidence 80
gate "tests + coverage floor" sh -c "uv run coverage run -m pytest -q && uv run coverage report"
if [ -n "${ENGINE_LINT_TOKENS:-}" ]; then
  gate "bleed-through lint, private tokens" uv run python -m core.evals.bleedthrough_lint
  printf '\n== wide scan, private tokens (reports, never fails, as in CI)\n'
  uv run python -m core.evals.bleedthrough_lint --wide || true
fi

if [ -n "$failed" ]; then
  printf '\nFAILED:%s\n' "$failed"
  exit 1
fi
printf '\nall gates passed\n'
