# The scheduled scripts take an extra's NAME, never a flag string
Date: 2026-09-16
Type: Two-way door

The container image installs without the `[claude]` extra: API keys only, no
Claude Code, no Max seat, which is row 7.26's exit criterion. The scheduled
scripts have always passed `uv run --extra claude`, and `uv run` syncs the
environment before it runs, so in that image the 08:00 job would ask uv to
resolve a package the locked sync deliberately left out, against a network the
box may not have.

The knob is `ENGINE_UV_EXTRA`, in `scripts/lib/uv-run.sh`: the NAME of the one
optional extra to sync. Unset (launchd sets nothing) keeps the Mac on
`claude`; empty means this host installed no extras and uv is asked for none.

**Why a name and not the flag string.** The obvious shape is
`ENGINE_UV_EXTRAS="--extra claude"` expanded unquoted as `uv run $UV_EXTRAS`.
It was written that way first, and row 7.20's same-behaviour test caught it:
**zsh does not word-split an unquoted parameter expansion**, so under the
shell launchd actually invokes, uv receives `--extra claude` as ONE argument
and the morning run dies on an unknown option. The bash half of the test
passed. An array splits correctly in both shells, but the bash macOS ships is
3.2, where an EMPTY array's expansion is fatal under `set -u` (row 7.20,
decision 2), and empty is exactly the container's case. `eval` would work and
puts the whole command line through a second round of parsing every morning.

Two fixed arguments behind an `if`, inside a wrapper function, is the shape
that behaves identically under both shells with no array and no eval:

    uv_run() {
      if [ -n "$UV_EXTRA" ]; then "$UV" run --extra "$UV_EXTRA" "$@"
      else "$UV" run "$@"; fi
    }

The cost is that a host can name one extra, not several. This repo defines two
(`claude`, `host`) and the scheduled scripts have only ever used one. A host
that needs more can point `UV` at its own wrapper.

`tests/unit/test_scheduled_scripts_linux.py` pins the Mac's argv under zsh AND
bash, with the knob unset and empty, because the whole point of the knob is
that the Mac's 02:00 and 08:00 runs do not move.
