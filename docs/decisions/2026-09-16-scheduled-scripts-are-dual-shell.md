# The scheduled scripts are dual-shell, and the plists keep invoking /bin/zsh
Date: 2026-09-16
Type: Two-way door

Row 7.20 had to make `engine-ap-daily.sh`, `auditor-nightly.sh` and
`ledger-backup.sh` run on Linux. They were written for one Mac and carried
its dialect: `#!/bin/zsh`, the zsh-only `${0:A:h:h}` path modifier, zsh's
`print -r --`, BSD `date -v-10d`, `UV=/opt/homebrew/bin/uv`, a macOS
per-user home fallback, and the first tenant's slug typed into every line. Under
bash today the daily script dies on line 17 with `A: unbound variable` and
**exits 0** having run nothing, which is the worst failure shape available:
a Linux host would report a clean morning and file no invoices.

The obvious fix is to point the launchd plists at `/bin/bash` and write
plain bash. This does not do that. The plists keep
`ProgramArguments = [/bin/zsh, <script>]`, and the scripts are written to
run correctly under **both** shells.

Why: the plists are rendered at install (row 7.25) and the rendered copies
already sit in `~/Library/LaunchAgents`. Changing the template changes
nothing on the Mac until the installer is re-run, so a bash-only script
merged with a bash template would run under the *old* zsh plist at 08:00
the next morning, on a machine nobody was watching. Dual-shell means the
merge cannot move the Mac whichever plist is live, and it means the same
file is the one CI exercises under bash. The shebang is `#!/usr/bin/env
bash` for the container, where the crontab execs the file directly.

What dual-shell forbids, written down so it is a rule and not a memory:

- no zsh path modifiers (`${0:A:h:h}`), no zsh `print`;
- **no arrays.** The bash macOS ships is 3.2, where expanding an EMPTY
  array under `set -u` is an unbound-variable error. The reconcile call
  used `STATEMENT_PARAM=()` for exactly the empty case, so the `--param`
  now rides an `if`/`else` instead;
- no assumption about which `date` is installed. Ten days back is a probe
  (`date -v-1d` succeeds on BSD, fails on GNU) and then the right flag.

Two resolutions changed binary-selection policy, in opposite directions,
on purpose:

1. **`uv` comes off PATH**, with `/opt/homebrew/bin/uv` kept as the
   last-resort fallback. The plists pin
   `PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`, and there is
   exactly one `uv` in those directories, so the Mac runs the same binary
   it always has. A test pins that resolution.
2. **`git` does NOT come off PATH.** The 23:00 push has always run
   `/usr/bin/git`, and Homebrew's `git` is first on that same PATH, so
   resolving off PATH would silently change which git pushes the ledger.
   The absolute path is preferred and PATH is only the fallback for a host
   that has no `/usr/bin/git`.

The tenant slug moved to `TENANT="${ENGINE_TENANT:-<first tenant>}"`.
launchd set no `ENGINE_TENANT`, so the Mac stayed on its tenant; the
container renders its own. **Refined at extraction (2026-09-28):** the
scripts assume no tenant at all. `scripts/lib/require-env.sh` stops an entry
script with exit 78, naming the variable, unless the host names
`ENGINE_TENANT`, `ENGINE_LEDGER_ROOT` and `AUDITOR_STORE_ROOT`; the first
tenant's plists name all three, and the ping library reads `ENGINE_HOST_ENV`
(the pre-extraction name stopped being read 2026-09-27, once every plist named `ENGINE_HOST_ENV`). The healthchecks slugs are built from it (`$TENANT-auditor-nightly`),
which reproduces the three existing slug strings exactly. A renamed check
goes silent instead of alerting, so a test pins the URLs the dead-man pings.
