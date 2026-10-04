# The tenant lives in its own repository, and the schedule names both checkouts
Date: 2026-09-22
Type: Two-way door

Gate 1 of the engine extraction plan (2026-09-21): the first tenant's folder moves
out of this repository whole, with its host layer, into a private repository
of its own, so that the public export is defined as whatever remains. The
plan said "point the engine at the new location"; this decision records what
that took, because the obvious version was wrong.

## Two roots, named once each, never inferred

Three wrappers under `tenants/<slug>/host/` found the engine by walking up
from their own file (`${0:A:h:h:h:h}`). That was correct only because they
lived inside the engine. The day the tenant moves out, the same walk lands in
the tenant checkout, which has no `core/` and no venv, and a wrapper that
sourced `$REPO/scripts/lib/hc-ping.sh` from there would fail somewhere past
the point where the failure names its cause.

So there are two roots and each is named by the plist:

- `ENGINE_REPO` = `{{REPO}}`, the engine's deploy clone, on every wrapper
  that lives with the tenant. The wrapper keeps the old walk as its fallback
  (a tenant still inside an engine checkout resolves the same way it always
  did) and refuses, exit 78, anything that is not an engine checkout.
- `ENGINE_TENANTS_ROOT` = `{{TENANT_REPO}}/tenants`, on every job that loads
  `tenant.toml`. The engine already honoured the name; the auditor read only
  its own `AUDITOR_TENANTS_DIR` and now falls back to the engine's name, re-read
  rather than imported, because the auditor imports nothing from `core`.

A wrapper that lives with the tenant exports the tenant root from its own
folder (`${TENANT_HOST:h:h}`) rather than trusting a default, so a hand run
from the tenant dev tree reads that tree's `tenant.toml`.

## The new repository mirrors the path, not the contents

The private repository holds `tenants/<slug>/...` at the same relative path
it had here. Flattening it (the repository root as the tenant folder) would
have touched every relative path in `tenant.toml`, every self-locating
script, and every test that pins one; mirroring touches none of them and
lets `ENGINE_TENANTS_ROOT` be a plain `<clone>/tenants`. The plan's "it is a
move, not a scrub" holds literally.

## Two deploy clones, one preflight

The tenant repository gets the engine's pattern: a dev checkout the owner
edits and PRs from (`~/Code/<tenant-repo>`) and a runtime clone
the plists point at (`~/Deploy/<tenant-repo>`). A single
checkout would have made an uncommitted tenant edit block the 08:00 run,
which is a failure mode the engine never had. The preflight guard freshens
the tenant clone with the same rules it applies to the engine's, reasons
prefixed `tenant-`, because a mute merged in the tenant repository has to
deploy the way a fix merged here does; a tenants root that is not inside a
checkout is left alone.

## What the installer refuses

The tenant's `install-launchd.sh` renders against the tenant repository's
path and stops before touching anything when that path holds no
`tenants/<slug>/tenant.toml`. A
plist rendered against an empty path loads without complaint and fails at
06:00. The order of the cut follows from this: merge this change, create and
clone the tenant repository, then re-run the installer, never the other way
round.

## Not decided here

Which of the ~10 Mac-pinning tests move to the tenant repository, and when
the tenant folder itself is deleted from this history, are gate 2 (the
scrub), not this change. The tenant's `skills/audit-triage` is a relative
link to the engine's skill and dangles once the tenant moves; the installer
now links the engine's copy directly, and the tenant repository deletes the
link at gate 2.

## Gate 2, first cut (2026-09-23): the stale twin goes, the tests follow the files

The scheduled runs proved the cut overnight (01:30, 02:00, 06:00, 08:00 all
rc=0 against the tenant clone), so the tenant folder left this tree. A test
now lives with the files it pins:

- **Moved to the tenant repository** (`tests/`, run against an engine checkout
  named by `ENGINE_REPO`): the launchd and installer suite, the qbo-sweep and
  build-lane wrappers, the vendor-port fidelity test, the build and qbo-sweep
  skill contracts with their fixtures, the build-note runner transcript, and
  the real tenant's case of every test that was parametrized over "the tenants in
  this repository" (collected in `tests/test_tenant_contract.py`).
- **Kept here**: every test of an engine file. The dead-man library, the
  daily script's stages, HOME pinning and the incident-log anchors
  (`tests/unit/test_entry_scripts.py`), the audit-triage skill contract, and
  each shipped-tenants test's `demo` case. Where a test needed two tenants it
  now builds the second from the demo in a temp directory.
- **The daily-stages check crossed the seam** and is the one that changed
  shape: it ran the engine's 08:00 script under a stub and compared against
  the tenant's `[auditor].expected_daily_jobs`. The tenant repository now reads the
  engine script's stage lines statically and makes the same one-way
  comparison; the engine keeps no copy, because the demo watches only its own
  two stages by design.
- `.coverage` (317 absolute paths of this Mac) is untracked and ignored.
