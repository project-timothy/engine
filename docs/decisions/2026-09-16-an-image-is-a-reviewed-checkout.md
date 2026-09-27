# A container image is a reviewed checkout; the freshness guard says so once
Date: 2026-09-16
Type: Two-way door

`scripts/run-preflight.sh` runs first in every scheduled entry script and
refuses to run code that is not reviewed `main`: on a branch, dirty, or unable
to fast-forward to `origin/main`, the job does not run and a marker lands for
the heartbeat lens. That guard was written for a deploy clone (issue #108) and
it is why a stale checkout cannot quietly run the morning.

A container image has no repository: it is a copy of the tree at a digest. The
guard would refuse `git-broken` and the container would never run a single
job. Three ways out were considered:

1. **Ship the repository inside the image.** Then every run tries to fetch an
   origin it cannot reach, takes the degrade path, and writes a `fetch-failed`
   warning marker three times a day. Every marker that is not a refusal becomes
   a nightly WARN finding, so the lens would nag forever about being a
   container and get muted. It also means a running container can pull new code
   into itself, which is the thing an image deploy exists to replace.
2. **Skip the guard in the container's crontab.** Then the container and the
   Mac no longer run the same files, and the crontab quietly carries a
   different contract than the plists.
3. **Let the image declare itself.** `ENGINE_IMAGE` carries the image
   reference or digest the entrypoint booted from. The guard takes that branch
   ONLY where there is no repository to check, reports what it is running, and
   exits 0.

Three was taken. The image digest IS the review: what is in the image was
built from a merged commit and cannot change while it runs, which is a
stronger guarantee than the guard's own.

Two properties the tests pin. The branch applies only where there is no
repository, so `ENGINE_IMAGE` can never switch the guard off on a real
checkout (setting it on a dirty tree still refuses `dirty-tree`). And it
writes NO marker, because the alternative is a nightly WARN that means "this
container is a container".

`engine doctor` reports the image on its own line, so a host that thinks it is
an image and is not, or the reverse, is visible in one command.
