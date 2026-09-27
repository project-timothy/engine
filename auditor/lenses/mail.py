"""Lens 3 — mail coverage: what arrived in the mailbox reached the engine.

With its own Graph listing (metadata only, content never downloaded), the
lens takes the 24 hours ENDING AT THE ENGINE'S LAST FETCH — mail newer
than that fetch is legitimately unprocessed until tomorrow's 08:00 — and
reconciles against the engine's ``mail.attachment_saved`` events:

- a non-denied message with a qualifying attachment (real extension, not
  inline, under the size cap) must have a saved event from the same sender
  domain at the same received time. A message with no story is a WARN (it
  may be a content-duplicate the engine rightly skipped; the detail says
  so) — this is "something arrived in the mailbox and nothing noticed";
- the privacy invariant, checked from the OTHER side: no saved event may
  exist from a denied sender's domain. That is a CRITICAL breach. The
  auditor inherits the privacy rule in full: denied mail appears in
  findings as counts and configured domains, never message identifiers or
  file names.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..clients.graph import AuditorGraphClient, ListedMessage, keychain_token_provider
from ..findings import Finding
from . import AuditContext

LENS = "mail"

SAVED_EVENT = "mail.attachment_saved"
WINDOW_HOURS = 24
# The auditor's own copy of the engine's intake conventions (fidelity
# contract): what counts as a document worth landing.
ALLOWED_EXTENSIONS = frozenset(
    {"pdf", "xlsx", "xls", "csv", "docx", "doc", "png", "jpg", "jpeg", "heic", "txt"}
)
MAX_BYTES = 30 * 1024 * 1024


def _domain(sender: str) -> str:
    return sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""


def _is_denied(sender: str, denied: list) -> bool:
    s = sender.lower().strip()
    return any(str(token).lower().strip() in s for token in denied if str(token).strip())


def _qualifies(message: ListedMessage) -> bool:
    for attachment in message.attachments:
        ext = attachment.name.rsplit(".", 1)[-1].lower() if "." in attachment.name else ""
        if not attachment.is_inline and ext in ALLOWED_EXTENSIONS and attachment.size <= MAX_BYTES:
            return True
    return False


def _saved_events(ctx: AuditContext) -> list[dict]:
    payloads = []
    for row in ctx.ledger.query(
        "SELECT payload_json FROM events WHERE tenant = ? AND event_type = ?",
        (ctx.tenant.slug, SAVED_EVENT),
    ):
        try:
            payloads.append(json.loads(row["payload_json"]))
        except json.JSONDecodeError:
            continue
    return payloads


def _last_fetch_time(ctx: AuditContext) -> datetime | None:
    rows = ctx.ledger.query(
        "SELECT created_at FROM runs WHERE tenant = ? AND agent = 'mail' AND job = 'fetch' "
        "AND shadow = 0 ORDER BY id DESC LIMIT 1",
        (ctx.tenant.slug,),
    )
    return datetime.fromisoformat(rows[0]["created_at"]) if rows else None


def check(ctx: AuditContext, client: AuditorGraphClient | None = None) -> list[Finding]:
    mail_config = ctx.tenant.raw.get("mail", {})
    if not mail_config:
        return []  # tenant has no engine mail fetch to audit
    anchor = _last_fetch_time(ctx)
    if anchor is None:
        return []  # heartbeat already covers a mail stage that never ran
    window_start = anchor - timedelta(hours=WINDOW_HOURS)
    denied = list(mail_config.get("denied_senders", []))
    saved = _saved_events(ctx)

    if client is None:
        client = AuditorGraphClient(keychain_token_provider(mail_config))
    listed = client.list_messages_with_attachments(
        since_iso=window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    findings: list[Finding] = []
    saved_keys = {(str(p.get("sender_domain", "")), str(p.get("message_date", ""))) for p in saved}

    for message in listed:
        received = datetime.fromisoformat(message.date.replace("Z", "+00:00"))
        if received > anchor:
            continue  # newer than the engine's last look; tomorrow's work
        if _is_denied(message.sender, denied):
            continue  # counts only, never identifiers; the breach check is below
        if not _qualifies(message):
            continue
        if (_domain(message.sender), message.date) not in saved_keys:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=f"mail from {_domain(message.sender)} at {message.date}",
                    condition="not-landed",
                    severity="WARN",
                    detail="the mailbox holds a message with a qualifying attachment but "
                    "the engine recorded no save for it; possibly a duplicate the engine "
                    "rightly skipped — verify before chasing",
                )
            )

    for payload in saved:
        domain = str(payload.get("sender_domain", ""))
        if domain and _is_denied(f"anyone@{domain}", denied):
            findings.append(
                Finding(
                    lens=LENS,
                    subject=f"denied sender {domain}",
                    condition="privacy-breach",
                    severity="CRITICAL",
                    detail=f"a save event exists from denied domain {domain}; the deny "
                    "filter failed and a file from a denied sender reached the landing "
                    "folder (count only; no identifiers reported)",
                )
            )
    return findings
