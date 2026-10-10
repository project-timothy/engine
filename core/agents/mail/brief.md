# Mail agent brief

## Mission

Own the mailbox-to-landing-folder feed so the downstream document agents
(AP, timesheets) receive business paperwork and nothing else. Built
2026-07-16 to replace two legacy feeders: an unfiltered cloud automation
that copied EVERY inbox attachment into the landing folder (including the
owner's medical documents — the origin incident), and a legacy pipeline
script that separately fetched, classified, and moved mail.

## What it does

- **fetch**: list inbox messages with attachments since a rolling window,
  download file attachments that pass the filters, and save them to the
  landing folder's top level, never overwriting (identical content is
  adopted; a true name collision gets a suffixed name). Every saved file
  is recorded as a ledger event keyed by content hash, which is also the
  dedup memory across re-sends and re-runs.

## Filters (tenant config, deterministic)

- **Sender denylist**: mail from a denied sender is skipped and counted.
  PRIVACY RULE: nothing identifying — no address, domain, subject, or
  filename — from a denied message may appear in any recorded payload,
  because the ledger event log syncs to a remote git host. Counts only.
- **Extension allowlist**: only document-type attachments land.

## Boundaries (never)

- Never mutates the mailbox: no moves, no mark-as-read, no folder filing,
  no sends, no deletes. (The legacy pipeline moved mail into bookkeeping
  folders; this agent deliberately does not. If folder filing is wanted
  later it is a separate, owner-approved job.)
- Never writes outside the landing folder, and only through the write
  guard.
- Never records denied-sender identifiers (see privacy rule).
- Never deletes or replaces an existing landing file.

## Escalation

- Auth failure (no silent token): anomaly ``mail.auth_failed`` — the
  owner must re-run the one-time device-code consent.
- Oversized attachment (> size cap): skipped and counted; the mail stays
  in the inbox untouched, so nothing is lost.

## Input / output contract

Input: tenant config ``[mail]`` (azure ids, keychain names, landing_dir,
since_days, allowed_extensions, denied_senders, max_bytes) or fixture
``--param messages_file`` for evals. Output: standard ``RunResult``;
events ``mail.attachment_saved`` (file, sender, sender_domain, sha256,
message date; the sha256 also ends the key). AP intake joins its sender
verdict to this record (#356). Shadow mode reports would-save lines and writes nothing.
