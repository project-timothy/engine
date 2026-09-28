# The non-Mac proof, 2026-09-16 (first pass)

Phase 7 row 7.26, the phase exit. The demo tenant ran the full daily loop, the
nightly audit, the ledger push, the heartbeat and the retry sweep in the
container on a Linux host that is not the Mac the engine was written on, with
no Claude Code, no Max seat, and no Claude Agent SDK in the image.

This is the first of two passes. The second, on a VPS, closed the phase:
`docs/non-mac-proof-2026-09-28.md`.

## The host

An OrbStack Linux machine on the Mini, created clean for this pass, with its
own Docker engine installed inside it from Ubuntu's archive. Nothing here ran
against the Mac's own Docker engine, its launchd jobs, the live ledger or the
auditor store.

    $ orb list
    engine726  running  ubuntu  resolute  arm64  640.1 MB  192.168.139.127

    $ uname -a
    Linux engine726 7.0.14-orbstack-00380-ga7e0a2dc9535 #1 SMP PREEMPT Fri Aug  7 03:48:40 UTC 2026 aarch64 GNU/Linux

    $ cat /etc/os-release | head -3
    PRETTY_NAME="Ubuntu 26.04.1 LTS"
    NAME="Ubuntu"
    VERSION_ID="26.04"

    $ docker version
    Client:
     Version:           29.1.3
     API version:       1.52
     Go version:        go1.24.13
     Git commit:        29.1.3-0ubuntu4.1
     Built:             Wed Apr 29 16:40:20 2026
     OS/Arch:           linux/arm64
    Server:
     Engine:
      Version:          29.1.3
      API version:      1.52 (minimum version 1.44)
      OS/Arch:          linux/arm64
     containerd:
      Version:          2.2.2
     runc:
      Version:          1.4.0-0ubuntu1

    $ docker compose version
    Docker Compose version 2.40.3+ds1-0ubuntu1

The repository was cloned over https at `b12ce04` (row 7.22 merged), and the
clone's remote was rewritten to a tokenless URL in the same command, so no
credential is stored on that box.

## The image

Built on the host by `docker compose up -d`, 15.8 s wall clock on nine cores.

    $ docker image inspect --format 'Id: {{.Id}}' engine:dev
    Id: sha256:20928b5d171703f0a5826e7b4b68e9bc773245f96c405966937c72772627983d

    $ docker image inspect --format 'Size: {{.Size}}' engine:dev
    Size: 136988856

137 MB of content, 564 MB on disk unpacked. The digest is the manifest list
the build exported; the image was never pushed to a registry, so it has no
registry digest of its own.

## The API-keys-only proof (row 7.26's criterion)

    $ docker compose exec engine uv run python -c "import claude_agent_sdk"
    ModuleNotFoundError: No module named 'claude_agent_sdk'

    $ docker compose exec engine sh -c 'command -v claude || echo "no claude binary"'
    no claude binary

    $ docker compose exec engine sh -c 'uv pip show claude-agent-sdk'
    warning: Package(s) not found for: claude-agent-sdk

    $ docker compose exec engine sh -c 'echo "ENGINE_UV_EXTRA=[$ENGINE_UV_EXTRA]"'
    ENGINE_UV_EXTRA=[]

The tenant's only model tier is `fixture`, which calls nobody. One throwaway
age identity was generated on the box and two variable names the demo tenant
declares were encrypted to it with placeholder values. No real key of any kind
was on that host.

## `engine doctor`, green

    $ docker compose exec engine uv run engine doctor demo
    doctor demo: 34 checks, 0 missing, 4 not configured
      ok       tenant config: /data/tenants/demo/tenant.toml loads (Non-Mac Proof LLC)
      ok       secret qbo_client_id: environment variable DEMO_QBO_CLIENT_ID comes from tenant.secrets.enc.yaml (the scheduled jobs carry it; this command's own environment does not)
      ok       secret qbo_client_secret: environment variable DEMO_QBO_CLIENT_SECRET comes from tenant.secrets.enc.yaml (the scheduled jobs carry it; this command's own environment does not)
      ok       secrets file: /data/tenants/demo/tenant.secrets.enc.yaml decrypts, carrying 2 variable(s)
      ok       secrets coverage: every variable [secrets] declares (2) is provided
      skip     model tier fixture: the fixture adapter calls no provider
      skip     accounting connection: [qbo].token_file is empty: no live connection on this host
      skip     mailbox: [mail].client_id is empty: the mail fetch is off
      ok       folder data/demo-data/inbox: data/demo-data/inbox
      ... (14 folder lines, all ok)
      ok       ledger: /data/ledger/demo
      ok       ledger remote: /data/ledger/demo has a remote to push to
      skip     dead-man pings: [host].healthchecks is false: this host is not watched off-box
      ok       image: running image engine:dev
      ok       supercronic: /usr/local/bin/supercronic
      ok       schedule engine: 0 8 * * * /app/scripts/engine-ap-daily.sh
      ok       schedule auditor: 0 2 * * * /app/scripts/auditor-nightly.sh
      ok       schedule ledger_backup: 0 23 * * * /app/scripts/ledger-backup.sh
      ok       schedule heartbeat: */30 * * * * /app/scripts/host-heartbeat.sh
      ok       schedule retries: */15 * * * * /app/scripts/engine-jobs-resume.sh
      ok       eval results: green results for inbox_classify, invoice_extract, receipt_extract, scan_group
    doctor rc=0

The name in the first line is the tenant file edited at step 3, so the edit and
the read-back are in that line too.

## The crontab

Rendered from `[host.schedule]` in the tenant file at every boot.

    CRON_TZ=America/New_York
    0 8 * * * /app/scripts/engine-ap-daily.sh >> /data/logs/engine-ap-daily.log 2>&1
    0 2 * * * /app/scripts/auditor-nightly.sh >> /data/logs/auditor-nightly.log 2>&1
    0 23 * * * /app/scripts/ledger-backup.sh >> /data/logs/ledger-backup.log 2>&1
    */30 * * * * /app/scripts/host-heartbeat.sh >> /data/logs/host-heartbeat.log 2>&1
    */15 * * * * /app/scripts/engine-jobs-resume.sh >> /data/logs/engine-jobs-resume.log 2>&1

The cycle below was watched on the smoke-test crontab, where every one of those
five lines is `* * * * *` and the commands are identical. The real schedule came
back with a plain `docker compose up -d`, and the five lines above are that box
after the restore.

## The daily loop

    [preflight:engine-ap-daily] ok: image engine:dev (no checkout here; the image is the reviewed artifact)
    === engine-ap-daily Wed Sep 16 14:56:00 EDT 2026 (since=2026-09-06) ===
    mail/fetch @ demo: error
      job failed: ValueError: no [mail] config: set client_id/tenant_id/keychain names in tenant.toml
      anomalies: 1
    ap/intake @ demo: ok
      intake: nothing to process
      commit: 07b9940984f3
    ap/apply @ demo: ok
      apply: filed 0; 0 already filed
      commit: c2f93fd93811
    ap/qbo-push @ demo: ok
      qbo-push: nothing to push
      commit: 39f1a5dc4849
    ap/qbo-push-payments @ demo: ok
      qbo-push-payments: off ([qbo].payment_records = false); no card, no write
      commit: b453f054a2d4
    statement tier: reading data/demo-data/statements
    ap/reconcile @ demo: error
      job failed: ValueError: no QBO token file: set [qbo].token_file in tenant.toml or pass --param qbo_token_file=PATH (or replay with --param evidence_file=PATH)
      anomalies: 1
    ap/workbook @ demo: ok
      workbook: wrote /data/demo-data/reports/ap-ledger.xlsx
      actions: 1
      commit: 1560b7dd6a49
    timesheets/intake @ demo: ok
      timesheets: recorded 0, flagged 0
      commit: ab46efeccac4
    expenses/inbox @ demo: ok
      expenses inbox: 0 receipt proposal(s), 0 skip proposal(s), filed 0, moved 0 to _not-receipts
      commit: 73989032aa87
    expenses/intake @ demo: ok
      expenses intake: filed 0, already-filed 0, duplicate 0, needs-attribution 0
      commit: ea6cad9be2c3
    expenses/extract @ demo: ok
      expenses extract: nothing pending
      commit: ba84a2f5d2c5
    ap/janitor @ demo: ok
      janitor: archived 0 aged file(s) (older than 10d)
      commit: 4cf2c5fafe3f
    === done: mail=1 intake=0 apply=0 qbopush=0 qbopushpay=0 statement=0 reconcile=1 workbook=0 timesheets=0 expinbox=0 expintake=0 expextract=0 janitor=0 ===

`mail` and `reconcile` are 1 because this box has no mailbox and no accounting
connection, which `engine doctor` reports as `skip` and `docs/install.md` step 8
says out loud. Every other stage is 0. Making those two stages skip when they
are unconfigured changes what the 08:00 run does, which is the owner's rule and
a different row.

Three cycles ran and printed that same `=== done:` line three times. The log
carries ten `commit:` lines in total, all of them from the first cycle, and the
ledger holds eleven commits: those ten plus the one `engine init` made. Runs two
and three re-executed every stage and wrote nothing, which is what an
idempotency key is for.

## The nightly audit

    [preflight:auditor-nightly] ok: image engine:dev (no checkout here; the image is the reviewed artifact)
    === auditor-nightly Wed Sep 16 14:56:00 EDT 2026 ===
    audit @ demo: 11 new, 11 open, 0 resolved
      report: data/demo-data/reports/_auditor/audit-2026-09-16.md
    === done: auditor=0 ===

Across the three cycles: `11 new, 11 open, 0 resolved`, then `1 new, 2 open,
10 resolved`, then `0 new, 2 open, 0 resolved`. The eleven were the heartbeat
lens saying no daily stage had ever run, which was true of a box minutes old
whose first audit fired in the same minute as its first daily loop. The report
on the volume:

    $ ls -l /data/demo-data/reports/_auditor/
    -rw-r--r-- 1 root root 3780 Sep 16 18:56 audit-2026-09-16.md

    # Auditor report — demo — 2026-09-16

    ## New since the last report

    Nothing new.

    ## Open checklist

    - [ ] CRITICAL since 2026-09-16 (heartbeat) daily run ap/reconcile: no run recorded at all for this daily stage; the stage is expected in config but has never executed
    - [ ] INFO since 2026-09-16 (recurrence) heartbeat never-ran: 11 subjects in 60 days (1 still open) ...

    ## Advisory

    All quiet in the books.

    (Counsel above is the deterministic fallback voice; no model drafted it. Reason: fixture.)

The one open CRITICAL is `ap/reconcile`, the stage that errors because there is
no accounting connection. The auditor is right and the box is honest about it.

## The ledger and its push

    === ledger-backup Wed Sep 16 14:56:00 EDT 2026 ===
    To /data/ledger-backup.git
     * [new branch]      main -> main
    === done: ledger push rc=0 ( * [new branch]      main -> main) ===
    === done: ledger push rc=0 (   1b4a125..4cf2c5f  main -> main) ===
    === done: ledger push rc=0 (Everything up-to-date) ===

    $ git -C /data/ledger-backup.git log --oneline | head -3
    4cf2c5f ap/janitor [62064088f4e8]: janitor: archived 0 aged file(s) (older than 10d)
    ba84a2f expenses/extract [3c896af880da]: expenses extract: nothing pending
    ea6cad9 expenses/intake [695b868f3ff6]: expenses intake: filed 0, already-filed 0, duplicate 0, needs-attribution 0

A bare repository on the volume stood in for the operator's real remote, the
same substitution the CI container job makes. The step 5 an operator follows now
is an https remote plus a credential helper, and both halves of that were run on
this box: the remote and the helper configured cleanly, and the helper answered

    $ git credential fill    (protocol=https, host=github.com)
    username=x-access-token
    password=<the value of LEDGER_PUSH_TOKEN>

The push to a real private https remote is the part this pass did not do, and
the VPS pass is where it belongs.

## The other two jobs

    $ cat /data/logs/engine-jobs-resume.log
    no retries due for demo

    $ ls -l /data/logs/host-heartbeat.log
    -rw-r--r-- 1 root root 0 Sep 16 18:56 /data/logs/host-heartbeat.log

The heartbeat log is empty and that is correct: no `HC_PING_BASE`, so the dead
man pings nothing and exits 0.

    $ docker compose logs | grep -E 'job succeeded|error running command'
    14:56:00 job succeeded  /app/scripts/host-heartbeat.sh
    14:56:00 job succeeded  /app/scripts/ledger-backup.sh
    14:56:00 job succeeded  /app/scripts/engine-jobs-resume.sh
    14:56:00 job succeeded  /app/scripts/auditor-nightly.sh
    14:56:01 error running command: exit status 1  /app/scripts/engine-ap-daily.sh

## What `docs/install.md` got wrong

Six things, all fixed in the same PR as this note. The first two stop a
stranger cold.

1. **Step 3 told you to use an editor the image does not have.** Naming the
   business was `docker compose exec engine vi /data/tenants/demo/tenant.toml`,
   and the image ships no `vi`, `vim`, `nano` or `ed`:

       OCI runtime exec failed: exec failed: unable to start container process:
       exec: "vi": executable file not found in $PATH

   The step is a `docker compose cp` round trip now: copy the file out, edit it
   with the host's editor, copy it back. Proven on this box, doctor reading the
   new legal name afterwards.

2. **Step 5 documented a git remote the image cannot reach.** The ledger's
   backup remote was `git@example.com:you/ledger.git`, and git shells out to
   ssh for that form:

       error: cannot run ssh: No such file or directory
       fatal: unable to fork

   There is no ssh client in the image and no key on the box. Every fresh
   install would have followed that step, gone green on `engine doctor` (which
   checks that a remote exists, not that it answers), and then failed its 23:00
   push every night. The step is an https remote now, with a credential helper
   that reads `LEDGER_PUSH_TOKEN` out of the environment the encrypted file
   fills, so the token is in no file on the volume.

3. **Step 7 invited a half-filled secrets file, which turns a green doctor
   red.** "Store the keys the lanes you want need" reads as permission to
   encrypt only what you are connecting. Doing exactly that with one of the
   demo tenant's two declared variables produced

       MISSING  secrets coverage: declared in tenant.toml, in neither the
                environment nor the encrypted file: DEMO_QBO_CLIENT_SECRET

   on a box that was otherwise perfect. That is the doctor working as designed,
   so the page now says it: put every declared variable in the file, or delete
   the ones you do not want from `[secrets]`.

4. **There was no way to find out whether the box worked until tomorrow.** The
   old last step was "check it in the morning". A new step 9 runs the whole
   schedule once a minute
   (`ENGINE_SCHEDULE_EVERY_MINUTE=1 docker compose up -d`), says which two lines
   to look for, and puts the real schedule back with a plain
   `docker compose up -d`. Both directions were run on this box.

5. **Step 1 said "copy this repository to the host" and left you there.** It is
   a `git clone` and a `cd` now.

6. **Nothing said your user needs to be in the `docker` group.** Every command
   on the page is unprefixed `docker`. The preamble says it, with `sudo` as the
   alternative.

Three smaller corrections rode along:

- The page claimed twenty minutes "most of it waiting for the image to build".
  The build was 15.8 s here. The claim is gone.
- Step 7 wrote the plaintext to `/tmp/plain.yaml` inside the container and
  deleted it afterwards. sops reads standard input instead now, so no cleartext
  file exists at any point, in the container or on the volume.
- `docs/credentials-checklist.md` offered sops' in-place edit, which opens
  `$EDITOR` on the plaintext and has nothing to open in this image. It now says
  to read the file and type the whole thing again, and it carries a
  `LEDGER_PUSH_TOKEN` row.

Three regression cases in `tests/unit/test_container_image.py` hold all of it:
the docs may only tell an operator to type a binary the Dockerfile puts in the
image, they may not document a git remote the image cannot reach, and the
install page has to name the coverage check.

## What this pass did not prove, and the VPS pass that is owed

Owed: the same walk on a $5 VPS. What it needs, and nothing more:

- **A host.** Any 1 GB Linux VPS with Docker and the Compose plugin. Nothing
  listens on a port, so no DNS and no firewall rule are needed.
- **The age identity generated on that box**, the same way step 6 does it here.
  The key never travels. If the box is rebuilt, the encrypted file is a brick
  and the keys get encrypted again.
- **A real private https remote for the ledger**, with `LEDGER_PUSH_TOKEN` in
  the encrypted file, so the 23:00 push is proven against a remote that can
  refuse it.

Three differences that pass would close: a one-core box builds the image in
minutes rather than seconds; the ledger push crosses a network; and the clock
runs long enough for the real 02:00 and 08:00 fires instead of a smoke-test
crontab.

## Teardown

The machine, its Docker engine, the image and the volume were destroyed after
this evidence was captured. Nothing on the Mac's own Docker engine, launchd
jobs, its live ledger or `.auditor` was touched at any point, and no
accounting or mailbox call was made from anywhere.
