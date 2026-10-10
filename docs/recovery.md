# Recover on a new box

The box is gone, or its volume is. The ledger comes back from its remote (the
23:00 push); everything else that lived only on the volume is rebuilt or
reconnected. Have these to hand: the ledger remote's address and a token for
it, your own copy of `tenant.toml`, and every secret value step 7 of
`docs/install.md` asked for.

What does not come back from the ledger: the box's age key, the encrypted
secrets file, the mailbox sign-in, the accounting token, and the documents
the engine filed. The last of those comes back only from a backup of the
`engine-data` volume.

1. Install the box: steps 1 and 2 of `docs/install.md`, with the same
   `ENGINE_TENANT` as before. The first boot makes a new, empty ledger; the
   next step replaces it.

2. Stop the scheduler and put the old ledger in place of the new one:

       docker compose stop
       docker compose run --rm engine sh -c \
         'rm -rf /data/ledger/demo && git clone -b main https://example.com/you/ledger.git /data/ledger/demo'

   Type the token when git asks for a password. Keep `-b main`: a remote
   whose own default branch is not `main` gives a plain clone an empty
   directory. The engine refuses to open it ("has a ledger's git history but
   no ledger.sqlite3") rather than start a fresh ledger there; re-clone with
   `-b main`.

3. Put your tenant file back:

       docker compose cp tenant.toml engine:/data/tenants/demo/tenant.toml

4. Make the box a new key and store the secrets again: steps 6 and 7 of
   `docs/install.md`. The old encrypted file opens only with the old key; if
   you kept that key off the box, copy it to `/data/age/keys.txt` and the old
   file instead, and skip the typing.

5. Teach the ledger remote to read its token again: the `credential.helper`
   line in step 5 of `docs/install.md`. The clone kept the remote's address.

6. Start the box and sign the mailbox in again:

       docker compose up -d
       docker compose exec -it engine uv run engine mail consent demo

7. Reconnect the accounting system the way you first connected it, and put
   the new token file where `[qbo].token_file` in the tenant file points.

8. Check it:

       docker compose exec engine uv run engine doctor demo

   The `ledger size` line counts the restored commits. A number near the old
   ledger's means the history came back; `0 commits` or `1 commit` means step
   2 cloned nothing, so do it again before any job runs.

The restore itself is a test: `tests/unit/test_ledger_restore.py` pushes a
ledger to a bare remote, clones it into an empty directory, and checks that
the tables and the event log agree and that the next run is a no-op.
