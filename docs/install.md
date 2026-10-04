# Install the engine on a Linux host

One container, one data volume. About twenty minutes. You need a Linux machine
(a $5 VPS is enough), Docker with the Compose plugin, a user in the `docker`
group (or type `sudo` in front of every `docker` command below), and a name for
the business this box will run.

## Install

1. Put this repository on the host and change into it:

       git clone <the repository's url> engine
       cd engine

2. Build the image and start the box:

       docker compose up -d

   The first boot creates the tenant, the ledger and the folder tree on the
   data volume, renders the schedule, and starts the scheduler. Watch it:

       docker compose logs -f

3. Name the business. The tenant file lives on the data volume and the image
   ships no editor, so copy it out, edit it with the host's editor, copy it
   back:

       docker compose cp engine:/data/tenants/demo/tenant.toml .
       nano tenant.toml
       docker compose cp tenant.toml engine:/data/tenants/demo/tenant.toml

   Set the legal name, the timezone, the people, and the account names you
   use. Everything the engine does comes from this file; nothing else needs
   editing. Put your own slug in `ENGINE_TENANT` in `compose.yaml` before the
   first boot if you want the tenant called something other than `demo`.

4. Ask what is still missing:

       docker compose exec engine uv run engine doctor demo

   Every line is either `ok`, `skip` (a lane you have not connected, which is
   fine), or `MISSING` with the fix in it. Step 5 closes the one a fresh box
   always has.

5. Give the ledger somewhere to back up to. The engine pushes it every night
   at 23:00, so it needs a remote: make an empty private repository of your
   own and point the ledger at its https address. The image carries no ssh
   client, so an ssh address cannot work.

       docker compose exec engine \
         git -C /data/ledger/demo remote add origin https://example.com/you/ledger.git

   A private repository wants a password too. Put a token in step 7's
   encrypted file as `LEDGER_PUSH_TOKEN` and teach this one remote to read it,
   so the token is in no file on the volume:

       docker compose exec engine git -C /data/ledger/demo config credential.helper \
         '!f() { printf "username=x-access-token\npassword=%s\n" "$LEDGER_PUSH_TOKEN"; }; f'

   The nightly push runs from the scheduler, which carries that variable; your
   own `docker compose exec` does not, so a push you run by hand asks for a
   password.

   If you do not want the nightly push, set `ledger_backup = ""` under
   `[host.schedule]` in the tenant file instead. Either answer satisfies
   step 4.

6. Make this box its own key. It is what unlocks the keys you are about to
   store, it lives here and nowhere else, and losing it means encrypting them
   again from scratch:

       docker compose exec engine sh -c 'mkdir -p /data/age && age-keygen -o /data/age/keys.txt && chmod 600 /data/age/keys.txt'
       docker compose exec engine grep 'public key' /data/age/keys.txt

   Write down the line it prints, the one beginning `age1`.

7. Store the keys, encrypted. Put your own `age1...` in the command, type one
   `NAME: value` per line using the variable names the tenant file's
   `[secrets]` and `[llm.tiers]` sections list, and end with Ctrl-D. Nothing
   is written in the clear at any point:

       docker compose exec engine sh -c 'umask 077; sops --encrypt --age age1YOURKEY \
         --input-type yaml --output-type yaml /dev/stdin \
         > /data/tenants/demo/tenant.secrets.enc.yaml'
       docker compose restart

   Put EVERY variable `[secrets]` names in that file, including a lane you
   have not connected yet. Once the file exists, the doctor's `secrets coverage`
   line calls a declared variable that is in neither the file nor the
   environment MISSING; the other way to answer it is to delete the ones you
   do not want from `[secrets]` in the tenant file.

   Step 4 now says how many decrypted. Never put a key in `tenant.toml` or in
   `compose.yaml`: those are the files people paste into support threads.
   `docs/credentials-checklist.md` covers changing one and rotating the key.

8. Connect the mailbox and the accounting system when you are ready. Until you
   do, the morning run reports those two stages as not configured and does
   everything else; `engine doctor` names them.

9. Prove it now, instead of waiting for tomorrow. This runs every scheduled
   job once a minute, same commands:

       ENGINE_SCHEDULE_EVERY_MINUTE=1 docker compose up -d

   Give it two minutes. `/data/logs/engine-ap-daily.log` ends with a
   `=== done: ...` line, `/data/logs/auditor-nightly.log` with
   `=== done: auditor=0`, and the day's report is under
   `/data/demo-data/reports/_auditor/`. Put the real schedule back with
   `docker compose up -d`.

10. Check it in the morning. The day's audit is on the volume at
    `/data/demo-data/reports/_auditor/`, and each job keeps its own log under
    `/data/logs/`. The first audit of a new box, taken before a daily run has
    landed, reports every stage as never executed; the next one after a run
    resolves them.

## What runs, and when

Times are in the timezone your tenant file names.

| When | What |
|---|---|
| 08:00 | the daily loop: mail, intake, filing, accounting writes, reconcile, workbook, timesheets, expenses |
| 02:00 | the independent nightly audit |
| 23:00 | the ledger's push to its remote |
| every 30 min | the off-box heartbeat, if you configured one |
| every 15 min | any job retries that are due |

To change a time, edit `[host.schedule]` in the tenant file and restart:
`docker compose restart`. The schedule is rendered from that file at every
boot, so editing the crontab on the volume has no effect.

## Day to day

    docker compose exec engine uv run engine queue list demo      # what needs a decision
    docker compose exec engine uv run engine queue approve demo 12
    docker compose exec engine uv run engine doctor demo          # what is missing
    docker compose logs -f                                        # the scheduler
    docker compose down                                           # stop; the volume stays

## Upgrading

    git pull
    docker compose up -d --build

The volume is untouched (tenant file, ledger, reports, logs); the new container renders the schedule again.
From an image before 2026-10 (it ran as root): the new one stops at boot and prints the one `chown` to run.

## Backups

The ledger's nightly push is the first tier and covers the thing that matters
most. For everything else, back up the `engine-data` volume on the host the
way you back up anything else on that machine.

Every step above was followed on a Linux host that is not the machine the
engine was written on: `docs/non-mac-proof-2026-09-16.md` (a local VM) and
`docs/non-mac-proof-2026-09-28.md` (a one-core VPS, overnight).
