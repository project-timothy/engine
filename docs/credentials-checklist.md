# Credentials checklist

Everything the engine needs a key for, where the key lives, and how to change
one. Two rules hold underneath all of it:

- **A tenant file names a variable. It never holds a value.** `tenant.toml`'s
  `[secrets]` table and each `[llm.tiers]` entry's `api_key_env` are names;
  `resolve_secret` reads them out of the process environment at the moment an
  adapter needs one. That is the whole contract, on every host, and it does
  not change.
- **How the environment gets filled is the host's business.** A laptop uses a
  shell. A Mac under launchd uses the job's own environment. A container has
  two options, and the encrypted one below is the one to use.

## What needs a key

| Lane | Variable | Where you get it |
|---|---|---|
| Accounting (QuickBooks Online) | `<PREFIX>_QBO_CLIENT_ID`, `<PREFIX>_QBO_CLIENT_SECRET` | the Intuit developer portal, one app per business |
| Mailbox (Microsoft Graph) | `<PREFIX>_GRAPH_CLIENT_ID` | an Entra app registration; then `engine mail consent <tenant>` once per host |
| Model tiers | whatever each `[llm.tiers].*.api_key_env` names | the provider's console. A `fixture` tier calls nobody and needs none |
| Dead-man pings | `HC_PING_BASE` | healthchecks.io, only if `[host].healthchecks` is true |
| Ledger backup | `LEDGER_PUSH_TOKEN` | your git host, only if the ledger's remote is https and private (`docs/install.md` step 5) |

`engine doctor <tenant>` lists every one of these with its name and whether
this host has it. It prints names, never values.

## The container: an encrypted file and an age key

`tenants/<slug>/tenant.secrets.enc.yaml` sits beside `tenant.toml` on the data
volume, encrypted with [sops](https://github.com/getsops/sops) to an
[age](https://github.com/FiloSottile/age) recipient. The container's entrypoint
decrypts it into the environment at boot, before anything else runs, and the
plaintext is never written anywhere. Both binaries are in the image, pinned by
version and checksum; you need nothing installed on the host.

The identity that opens the file lives at `/data/age/keys.txt` on the box and
nowhere else. Back it up the way you back up a password, not the way you back
up a repository: without it the encrypted file is a brick.

### Generate the box's identity (once)

    docker compose exec engine sh -c 'mkdir -p /data/age && age-keygen -o /data/age/keys.txt && chmod 600 /data/age/keys.txt'

Write the public key down. It is the line beginning `age1`, and it is what you
encrypt to:

    docker compose exec engine grep 'public key' /data/age/keys.txt

### Encrypt the tenant's secrets

One `NAME: value` per line, using the variable names from `[secrets]` and
`[llm.tiers]`. sops reads the plaintext on standard input and writes only the
ciphertext, so no cleartext file exists at any point:

    docker compose exec engine sh -c 'umask 077; sops --encrypt --age age1YOURKEY \
      --input-type yaml --output-type yaml /dev/stdin \
      > /data/tenants/acme/tenant.secrets.enc.yaml' <<'EOF'
    ACME_QBO_CLIENT_ID: the-client-id
    ACME_QBO_CLIENT_SECRET: the-client-secret
    ACME_MODEL_KEY: the-provider-key
    EOF

    docker compose restart
    docker compose exec engine uv run engine doctor acme

Doctor's `secrets file` line says how many variables decrypted, and
`secrets coverage` names any variable the tenant declares that is in neither
the file nor the environment. Neither line ever prints a value.

### Change one

Type the whole file again. The image ships no editor, so sops' in-place edit
(`sops <file>`, which opens the plaintext in `$EDITOR`) has nothing to open
inside the container. Read what is in it now with
`docker compose exec engine sops --decrypt /data/tenants/acme/tenant.secrets.enc.yaml`
(that prints the values, so use a terminal you would type a password into),
then re-run the encrypt command above with every line you want and restart.

### Rotate the age identity

Generate a second identity, re-encrypt the file to BOTH recipients
(`--age key1,key2`), swap the key file, confirm `engine doctor` is green, then
re-encrypt to the new one alone. Doing it in that order means the box is never
one command away from being unable to read its own secrets.

## The other option, and when it is fine

The entrypoint also reads a plain `/data/secrets.env` (mode 600, `NAME=value`
per line) and honours compose's `environment:` and `--env-file`. That is fine
for a box you own on hardware you own, and it is what row 7.21 shipped. The
encrypted file is what survives someone reading the volume: a backup, a
snapshot, a support session, a disk that leaves the building. Where both are
present the encrypted file wins.

## What never happens

- A value never goes in `tenant.toml`, `compose.yaml`, or any file in this
  repository. Those are the files people paste into support threads.
- A value never reaches a log, a card, an event, or the ledger. The runner
  redacts every job's output against this tenant's declared variables and
  against the shapes of known token families before a single row is written
  (`core/redact.py`).
- The engine never writes a secret anywhere. It reads one when an adapter
  asks and passes it straight to the wire.
