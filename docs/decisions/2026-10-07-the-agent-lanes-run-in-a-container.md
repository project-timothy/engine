# The agent lanes run in a container: three walls, no engine code in the image
Date: 2026-10-07
Type: Two-way door

A lane session (audit triage today, the build lane when it is loaded) writes
code and runs it. On the host it ran as the owner: the owner's home, keychain
and tokens in reach, and open egress (issue #353; #359's design note,
2026-10-04). The lanes move into a container, `Dockerfile.lane` run by
`compose.lane.yaml`, with three walls:

- **Mounts.** Five paths, all named by the tenant's wrapper: a fresh clone of
  the engine and of the tenant (rw), the staged inputs and a ledger snapshot
  (ro), and an output folder. Never the token files, the keychain, the host
  config folder or the live ledger. The tests name each one.
- **Egress.** The lane's only network is `internal: true`. The one door is a
  digest-pinned squid allowing github.com, api.github.com and
  api.anthropic.com; everything else gets a 403 and a log line the wrapper
  keeps. A host the session turns out to need is a reviewed PR to
  `host/lane/squid.conf`, never a wildcard.
- **Environment.** Only what the compose file lists; no `env_file`. The
  model credential (`CLAUDE_CODE_OAUTH_TOKEN`) and a fine-grained GitHub
  token pass through by NAME from the wrapper, which alone reads the file
  holding them. This closes the environment half of #386 by construction.

Four choices the design note left open:

**A separate Dockerfile, not a stage of the product's.** A target added to
`Dockerfile` would become the default build output, and a locally built
engine image cannot be pinned by digest for a `FROM`. `Dockerfile.lane`
repeats the two pinned bases instead, and the digest test reads both files.
The product image stays SDK-free (row 7.26).

**No engine code in the image.** It installs the lockfile's dependencies
(with the `[claude]` extra and the dev group) and nothing else. The code is
the mounted clone, so what a session tests is what it is reviewing. The
Claude Code CLI comes inside the `claude-agent-sdk` wheel, hash-pinned by
`uv.lock`; no Node, no installer script, and it moves only with the SDK pin.

**A `uv` shim instead of image environment variables.** The runner hands a
model's tool calls `PATH` and `HOME` only (`reduced_env`), so an `ENV` line
never reaches `uv run pytest`, which would then build a venv from PyPI and
hit the proxy. `host/lane/uv` sits first on `PATH`, names the image's venv,
and forbids a sync, a download and the network.

**Fresh clones, not worktrees.** A worktree's `.git` is a file naming a path
on the host, which does not exist in the container. The wrapper makes a
self-contained clone each run (`git init` plus a fetch of `origin/main`
from the local checkout, so the eval-first gate still has its base ref) and
deletes it afterwards unless it carries work.

Proven on the Mini before merge, from inside the container: uid 10001, no
host home in view, the bundled CLI runs, the Anthropic and GitHub APIs reach
through the proxy, PyPI and an arbitrary host are refused with 403, no direct
route out, inputs, ledger snapshot and venv read-only, the ledger snapshot
opens (`Ledger.open` needs its `.git` beside the database), pytest and ruff
run under the reduced environment, and no venv is built in the clone.

Order from here (the design note's): triage runs in shadow beside the host
lane for a week, notes compared; then cutover, which adds `gh` to the image
and passes `GH_TOKEN` to the tools that need it; then `chmod 700 ~` and the
token file modes; then the build lane.
