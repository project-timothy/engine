# sops delivers the environment, and the redactor names shapes instead of guessing entropy
Date: 2026-09-16
Type: Two-way door

Row 7.22 asked for two things that look separate and are the same thing: an
encrypted secrets file for the container, and the W-9 TIN rule extended to
tokens. The first creates keys the box holds; the second keeps them out of the
one file the box copies off itself every night.

## 1. The encrypted file is a delivery mechanism, not a second source

`resolve_secret` reads `os.environ`. It did before this row and it does after,
and `tests/unit/test_secrets_sops.py` reads its source to say so. Two designs
were possible and only one of them is safe:

- **sops as a fallback inside `resolve_secret`.** Then the same tenant resolves
  differently on a box that has an encrypted file than on one that does not,
  every adapter acquires a shell-out on its hot path, and a decryption failure
  surfaces as whatever exception the adapter happens to raise at 08:00.
- **sops in the entrypoint, before Python starts.** The decrypted names are in
  the environment of pid 1, every scheduled job inherits them, and the engine
  is unchanged. A box with no file, no key and no sops binary is not a broken
  box: it is a host that fills its environment some other way, which is every
  host this engine has ever run on.

The second. The consequence to know is that `docker compose exec` does NOT go
through the entrypoint, so an operator's own command sees the image's
environment and not the decrypted one. That is the correct boundary (a value
that showed up in a plain `exec env` would be readable by anything that can
reach the daemon) and CI asserts both halves of it: present in `/proc/1/environ`,
absent from `docker compose exec engine env`.

## 2. The plaintext is never a file, and never even a whole variable

The entrypoint streams `sops --decrypt --output-type dotenv` through a process
substitution into a `while read` loop running in the boot shell itself. No
temporary file, no tmpfs, no here-string (bash writes those to disk on the
versions this image may carry), and no `$(...)` capture holding the lot. sops'
exit code rides the stream on a last `__sops_rc=` line, because a pipeline's
status belongs to the wrong process and the loop has to run in this shell for
the exports to survive.

A failed decrypt does not stop the box, for the reason row 7.21 gave for the
doctor: a container that exits on a bad input is a crashloop, and the fix is an
edit to the volume the box is serving. It says so loudly, runs sops a second
time with stdout discarded to get the REASON into the log without the content,
and `engine doctor` is where the state of it lives.

## 3. Doctor gets names and a count, by construction

`core/engine/secrets.py:probe()` returns a status, one line, and the NAMES. It
has no accessor that hands back the mapping, so no future doctor line can print
a value by accident. `decrypt()` exists for that, and its only callers are the
probe (which immediately throws the values away) and `load_into`.

Four states, and the fourth is the one that pays for the row: `skip` when
neither file nor key is here, `missing` naming whichever half is absent,
`missing` carrying sops' own first line when it will not open, and `ok` with
the count. Beside it, `secrets coverage` names any variable `tenant.toml`
declares that is in neither the environment nor the file. That check is scoped
to hosts that use this lane, so a host without a file keeps exactly the
behaviour it had before this row: the per-secret lines say which variables are
set and none of them fails the install.

## 4. No entropy rule. The measurement said so.

The row's acceptance asks for "token-shaped strings", and the obvious reading
is a high-entropy rule over 32-character runs. That was measured first, over
the identifiers this engine actually writes into events:

| string | bits/char |
|---|---|
| `Invoice_2026-09-16_Acme_Fabrication_PO` | 4.37 |
| `COGS-Project_Expense-PN00_0412_Subaccount` | 4.55 |
| `DocumentIdentifier0123456789ABCDEFGHIJ` | 4.86 |
| random 32-char base64url tokens (30 samples) | 4.33 to 4.90 |

The distributions overlap end to end. Any threshold either shreds file names
and account names or misses real tokens, and a redactor that eats a `sha16`
card key makes a decided card re-park every night. So four rules that do not
guess shipped instead:

1. the VALUE of every variable `tenant.toml` declares as a secret, replaced by
   `<redacted:NAME>`. The only airtight rule, and the one that catches a key
   with no recognisable shape at all;
2. named token families: `sk-`, GitHub, Slack, AWS, Google, JWT, `Bearer` and
   `Basic`. No identifier here begins with any of those;
3. TIN shapes, unchanged;
4. a WHOLE value that is one padded base64 blob. `+` and `=` padding are
   characters no identifier in this engine contains, which is why this one
   generic rule survives the measurement above.

Hex is never redacted at any length: sha16 content keys, sha256 digests,
`stmt:` ids and run keys are all hex. Rule 4 reads a whole value and never a
run inside a sentence, because scanning prose for opaque runs is what turns a
redactor into a shredder.

The gap this leaves, stated plainly: an unpadded base64url token that arrives
inside prose from a provider nobody here has met is not caught. The answer to
that is rule 1, which requires the tenant to declare the variable, and
declaring it is already how the engine finds it.

## 5. One redactor, and it moved

`core/llm/transcript.py` had these rules for session transcripts. The runner
now applies them to every `JobOutput`, so the module moved to `core/redact.py`,
which imports nothing: `core.engine` and `core.llm` both use it and neither may
depend on the other. `core.llm.transcript` re-exports the same names and every
existing caller is untouched.

The runner applies it at one place, after `handler.run(ctx)` and before a
single row is written, and to the failure results it builds itself. One place
rather than every job, because a rule that has to be remembered at three
hundred call sites is a rule that is already broken somewhere. `EventSpec.key`
and `ApprovalSpec.key` are deliberately NOT passed through it: they are content
fingerprints the ledger keys on, and moving one would re-park a decided card.
