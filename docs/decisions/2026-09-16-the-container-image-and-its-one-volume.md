# The image carries code, the volume carries everything else
Date: 2026-09-16
Type: Two-way door

Row 7.21's second half: `Dockerfile`, `compose.yaml`, `host/entrypoint.sh`, a
CI job that watches one whole cycle, and `docs/install.md`.

**One volume at `/data`, and `/app/data` is a symlink to it.** The volume holds
the tenant file, the ledger, the auditor store, the reports and the logs, so
`docker compose down` destroys nothing and an image update overwrites no
tenant. The symlink is what keeps the tenant file's paths readable: every path
in `tenant.toml` is relative to the directory the engine runs from, the
scheduled scripts `cd` to `/app`, so the first boot renders `data/<slug>-data/`
and a person reading the file sees `data/...` rather than `../data/...`.

**The tenant is created at first boot, not baked into the image.** The
entrypoint runs `engine init` (row 7.19) when the tenants root has no such
tenant, which is what makes the image identical for every business and the
volume the only thing that differs.

**The entrypoint's order is the decision**: make the volume's shape, put
secrets in the environment, create the tenant if this is a first boot, render
the crontab, run the doctor, exec supercronic. Secrets come before the doctor
because the doctor's whole job is to report what is missing, and it would
report every one of them missing on a box that has them. Row 7.22 lands in
that step: it decrypts `tenant.secrets.enc.yaml` with an age key held only on
the box, replacing the plain `/data/secrets.env` this row reads today.

**The crontab is rendered at every boot** and a doctor that finds something
missing does NOT stop the box. A container that exits on a missing optional
item is a crashloop, and most of the fixes are edits to the tenant file the box
is serving. The report goes to the container log and the loop starts.

**An argument runs that command instead of the scheduler**, so
`docker compose run --rm engine uv run engine queue list <tenant>` gets exactly
the environment the scheduled jobs get.

**Every real tenant folder is in `.dockerignore`** (since extraction gate 2,
every `tenants/*` folder but `demo/` and `_templates/`). A distributed image must not
carry one business's vendor list, account names, paths, or Mac wiring. So are
`tests/`, `.git/`, and every state directory: `.git` twice over, because an
image that carried it would let the freshness guard think it had a checkout to
pull.

**supercronic is pinned by version and by sha256 per architecture**, verified
in the build. v0.2.49, amd64 `a53ae236...`, arm64 `02aa0cb2...`, computed from
the published artifacts on 2026-09-16 (aptible publishes no checksums file).
`uv` is copied from its own published image at the version CI installs. No
Python dependency is added: the lockfile is the list, and `uv sync --locked`
runs WITHOUT `--extra claude`, which is row 7.26's exit criterion.

## Two bugs the container found, both fixed here

**The ledger was born on `master` on any host that never set
`init.defaultBranch`,** while `ledger-backup.sh` pushes `origin main`. Every
fresh install's 23:00 job would have failed forever with "src refspec main does
not match any". `ensure_repo` now sets the branch itself. The Mac is untouched:
its ledger exists and the function returns early on an existing repository.

**A plain `docker compose exec ... engine doctor` lied.** The state roots were
exported by the entrypoint only, so an operator's own command looked for the
ledger at `/app/.ledger` and reported it missing on a healthy box. They are in
the image environment now, with the entrypoint keeping its fallbacks.

## What is NOT fixed here, on purpose

On a fresh tenant the morning run reports `mail=1 reconcile=1`: those two
stages fail because no mailbox and no accounting system are connected yet.
Making them skip-when-unconfigured changes what the 08:00 run does, which is
the owner's rule, not this row. `docs/install.md` says it plainly and
`engine doctor` names both lanes as `skip`, so nothing is hidden.
