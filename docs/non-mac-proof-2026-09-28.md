# The non-Mac proof, 2026-09-28 (second pass)

Phase 7 row 7.26, the phase exit, second and final pass. The first pass
(`docs/non-mac-proof-2026-09-16.md`) ran in a Linux VM on the development Mac
and left three things owed: a one-core box that builds the image in minutes,
a ledger push that crosses a network to a remote that can refuse it, and a
clock that runs long enough for the real nightly and morning fires. This pass
closes all three.

## The host

A cloud VPS created for this pass and destroyed after it: one vCPU, 1 GB of
memory, 25 GB disk. Nothing else ran on it.

    timothy-726-proof
    Ubuntu 24.04.4 LTS
    Linux 6.8.0-124-generic x86_64
    1 CPU, 961 MB

Docker 29.1.3 and the Compose plugin 2.40.3 from Ubuntu's archive. The box
was driven as a plain user in the `docker` group.

## The walk

`docs/install.md`, step by step, as a stranger would take it:

1. `git clone https://github.com/project-timothy/engine.git`: the public
   repository, commit `214f76f`.
2. `docker compose up -d`: the image built on the one core and the first boot
   created the demo tenant, its ledger and the schedule. The doctor, run by
   the entrypoint, named one missing item: the ledger's remote.
3. The legal name edited with the copy-out, edit, copy-back round trip.
4. The doctor, again.
5. An https remote for the ledger: a private repository made for this test,
   with the credential helper reading `LEDGER_PUSH_TOKEN`.
6. The box's own age key, generated on the box.
7. The secrets encrypted with sops from standard input. The ledger token was a
   fine-grained token scoped to that one repository with contents read and
   write, seven days to expiry, typed at a hidden prompt, so it never existed
   as a file. The doctor afterwards:

        doctor demo: 34 checks, 0 missing, 4 not configured

8. Mailbox and accounting left unconnected, as the demo intends.
9. `ENGINE_SCHEDULE_EVERY_MINUTE=1 docker compose up -d` for the smoke run,
   then `docker compose up -d` to put the real schedule back.

## The image

    engine:dev  sha256:513fcc6e8f91ed939d9d1893937156e1edfec1deca4c57a67b828662735ed731  577 MB

No optional extras: `claude-agent-sdk` is not installed in it (`uv pip list`
finds it zero times). API keys only; no Claude Code, no Max seat.

## The crontab

    CRON_TZ=America/New_York
    0 8 * * * /app/scripts/engine-ap-daily.sh >> /data/logs/engine-ap-daily.log 2>&1
    0 2 * * * /app/scripts/auditor-nightly.sh >> /data/logs/auditor-nightly.log 2>&1
    0 23 * * * /app/scripts/ledger-backup.sh >> /data/logs/ledger-backup.log 2>&1
    */30 * * * * /app/scripts/host-heartbeat.sh >> /data/logs/host-heartbeat.log 2>&1
    */15 * * * * /app/scripts/engine-jobs-resume.sh >> /data/logs/engine-jobs-resume.log 2>&1

## The logs: the real fires

The ledger push, 23:00, across the network to a private remote that requires
the token:

    === ledger-backup Sun Sep 27 23:00:00 EDT 2026 ===
    === done: ledger push rc=0 (   6ccd883..3eb92b7  main -> main) ===

The nightly audit, 02:00, and its report on the volume:

    === auditor-nightly Mon Sep 28 02:00:00 EDT 2026 ===
    === done: auditor=0 ===
    /data/demo-data/reports/_auditor/audit-2026-09-28.md

The daily loop, 08:00:

    === engine-ap-daily Mon Sep 28 08:00:00 EDT 2026 (since=2026-09-18) ===
    === done: mail=1 ar=0 intake=0 apply=0 qbopush=0 qbopushpay=0 statement=0 reconcile=1 sweepcards=0 workbook=0 timesheets=0 expinbox=0 expintake=0 expextract=0 janitor=0 ===

`mail=1` and `reconcile=1` are the two lanes step 8 leaves unconnected; both
jobs stop with a message naming the missing setting, the same as the first
pass. The container ran twelve hours without a restart.

## What this pass found

- **The unconnected lanes log `error`, and the guide says `not configured`.**
  Issue #366.
- **A fresh VPS may need `apt-get update` before Docker installs.** The image's
  package index was stale and one dependency returned 404; the update fixed it.
- **The token prompt must take a paste cleanly.** A terminal's bracketed-paste
  markers reached the first attempt and sops refused the file; the prompt now
  keeps only token characters and writes nothing unless encryption succeeds.

## Teardown

The VPS, its SSH key, the test repository and the token were removed after
this evidence was captured.
