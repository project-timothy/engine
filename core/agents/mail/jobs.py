"""Mail agent jobs: fetch.

Deterministic end to end: filters are config, dedup is content hash, and
the mailbox is never mutated. See ``brief.md`` for the privacy rule this
module enforces: denied senders leave counts, never identifiers.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from ...authority import CardRule
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobHandler, JobOutput
from ...engine.fileops import CannotVerify, CopyMismatch, place_bytes
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from .schema import (
    DEFAULT_ALLOWED_EXTENSIONS,
    MailMessage,
    extension_allowed,
    sender_address,
    sender_domain,
    sender_is_denied,
)

SAVED_EVENT = "mail.attachment_saved"
# A recognized credit-card charge receipt, filed to the expenses tree's
# _cc_charges/ (expenses design decision 3). A DIFFERENT event type on purpose: the
# auditor's mail lens reconciles SAVED_EVENT against AP intake, and filed
# cc paper must never read as a not-landed invoice.
CC_FILED_EVENT = "mail.cc_charge_filed"

# Listing fetched during key computation, handed to run() for the same ctx
# (the runner calls key() then run() once per context; fetching twice could
# hash different mailbox state than the run acts on).
_FETCH_CACHE: dict[int, list[MailMessage]] = {}


def _landing_dir(ctx: JobContext) -> Path:
    configured = ctx.params.get("landing_dir") or ctx.tenant.mail.landing_dir
    if not configured:
        raise ValueError(
            "no landing directory: set [mail].landing_dir in tenant.toml "
            "or pass --param landing_dir=PATH"
        )
    return Path(configured)


def _denied_senders(ctx: JobContext) -> list[str]:
    override = ctx.params.get("denied_senders")
    if override is not None:
        return [t for t in str(override).split(",") if t.strip()]
    return list(ctx.tenant.mail.denied_senders)


def _allowed_extensions(ctx: JobContext) -> frozenset[str]:
    configured = ctx.tenant.mail.allowed_extensions
    return frozenset(configured) if configured else DEFAULT_ALLOWED_EXTENSIONS


def _messages(ctx: JobContext) -> list[MailMessage]:
    """Fixture file (evals, replay) or the live Graph adapter."""
    import json

    override = ctx.params.get("messages_file")
    if override:
        raw = json.loads(Path(override).read_text())
        return [MailMessage(**m) for m in raw]

    from datetime import date, timedelta

    from ...adapters.graph_mail import GraphMailClient, keychain_token_provider

    mail = ctx.tenant.mail
    if not mail.client_id:
        raise ValueError("no [mail] config: set client_id/tenant_id/keychain names in tenant.toml")
    client = GraphMailClient(
        token_provider=keychain_token_provider(
            client_id=mail.client_id,
            tenant_id=mail.tenant_id,
            scopes=list(mail.scopes),
            keychain_service=mail.keychain_service,
            keychain_account=mail.keychain_account,
        )
    )
    since = ctx.params.get("since") or (date.today() - timedelta(days=mail.since_days)).isoformat()
    messages: list[MailMessage] = []
    for m in client.list_messages(since=since):
        atts = client.list_attachments(m["id"])
        messages.append(
            MailMessage(
                id=str(m["id"]),
                sender=str(((m.get("from") or {}).get("emailAddress") or {}).get("address", "")),
                date=str(m.get("receivedDateTime", "")),
                attachments=[
                    {"id": str(a["id"]), "name": str(a.get("name", "attachment.bin"))} for a in atts
                ],
            )
        )
    # Live attachments download lazily in run(); stash the client for it.
    _LIVE_CLIENTS[id(ctx)] = client
    return messages


_LIVE_CLIENTS: dict[int, object] = {}


def _attachment_bytes(ctx: JobContext, message: MailMessage, att) -> bytes:
    if att.content_b64:
        return base64.b64decode(att.content_b64)
    client = _LIVE_CLIENTS.get(id(ctx))
    if client is None:
        raise RuntimeError("no live mail client for byte download")
    return client.download(message.id, att.id)


def _fetch_key(ctx: JobContext) -> str:
    """Same mailbox listing + same filters = same run. The listing (ids
    only) is cheap; content hashes enter through the saved-event dedup."""
    messages = _messages(ctx)
    _FETCH_CACHE[id(ctx)] = messages
    key = RunKey(ctx, "mailfetch")
    listing = sorted(",".join(a.id for a in m.attachments) + ":" + m.id for m in messages)
    key.value("messages", listing)
    key.config("mail", "expenses.filing_dir")  # filters, senders, size cap, the cc route
    for name in (
        "landing_dir",
        "max_bytes",
        "expenses_filing_dir",
        "denied_senders",
        "cc_charge_senders",
    ):
        key.param(name)
    return key.digest()


def _seen_hashes(ctx: JobContext) -> set[str]:
    seen: set[str] = set()
    for e in ctx.ledger.read_event_log():
        if e.get("event_type") in (SAVED_EVENT, CC_FILED_EVENT):
            digest = str(e.get("idempotency_key", "")).rsplit(":", 1)[-1]
            if len(digest) == 64:
                seen.add(digest)
    return seen


def _cc_senders(ctx: JobContext) -> list[str]:
    override = ctx.params.get("cc_charge_senders")
    if override is not None:
        return [t for t in str(override).split(",") if t.strip()]
    return list(ctx.tenant.mail.cc_charge_senders)


def _cc_charges_dir(ctx: JobContext, message_date: str) -> Path | None:
    """`<expenses filing tree>/<month>/_cc_charges`, or None when the tenant
    has no expenses tree configured (the route then falls back to landing)."""
    filing = ctx.params.get("expenses_filing_dir") or ctx.tenant.expenses.filing_dir
    if not filing:
        return None
    month = str(message_date)[:7] if len(str(message_date)) >= 7 else ""
    if not month:
        from datetime import UTC, datetime

        month = datetime.now(UTC).strftime("%Y-%m")
    return Path(filing).expanduser() / month / "_cc_charges"


def _save(landing: Path, name: str, body: bytes, *, guard, shadow: bool) -> Path | None:
    """Never-overwrite, read-back-verified save (the core.engine.fileops
    contract, honesty audit 2026-09-03 03-F4). Returns the destination, or
    None in shadow (an adopted identical file is returned either way).
    Raises CannotVerify for a placeholder at the destination and
    CopyMismatch when the write reads back different content."""
    dest = landing / name
    if dest.resolve().parent != landing.resolve():
        # The schema already reduces a name to one segment; this is the
        # belt to that brace (security review 2026-10-03, finding 6).
        raise ValueError(f"attachment name {name!r} leaves the landing folder")
    placed = place_bytes(body, dest, guard=guard, shadow=shadow)
    if shadow and not placed.adopted:
        return None
    return placed.dest


def _save_anomaly(name: str, exc: OSError) -> Anomaly:
    """A save that could not be verified is not recorded: the message stays
    unseen and the next fetch retries it."""
    if isinstance(exc, CannotVerify):
        return Anomaly(
            code="mail.save_deferred",
            detail=f"{name}: {Path(exc.filename).name} at the landing folder is a cloud-only "
            "placeholder; cannot verify, retried on a later run",
        )
    return Anomaly(
        code="mail.save_unverified",
        detail=f"{name}: the saved file read back different content; removed, "
        "retried on a later run",
    )


def _fetch_run(ctx: JobContext) -> JobOutput:
    messages = _FETCH_CACHE.pop(id(ctx), None)
    if messages is None:  # defensive: key() always ran first under the runner
        messages = _messages(ctx)
    landing = _landing_dir(ctx)
    denied = _denied_senders(ctx)
    cc_senders = _cc_senders(ctx)
    allowed = _allowed_extensions(ctx)
    max_bytes = int(ctx.params.get("max_bytes") or ctx.tenant.mail.max_bytes)
    seen = _seen_hashes(ctx)

    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []
    saved = denied_count = filtered = dup = oversize = cc_filed = 0

    try:
        for message in messages:
            if sender_is_denied(message.sender, denied):
                # PRIVACY RULE: count it; record nothing else. No filename,
                # no sender, no subject — the event log leaves this machine.
                denied_count += 1
                continue
            for att in message.attachments:
                if not extension_allowed(att.name, allowed):
                    filtered += 1
                    continue
                body = _attachment_bytes(ctx, message, att)
                if len(body) > max_bytes:
                    oversize += 1
                    continue
                digest = hashlib.sha256(body).hexdigest()
                if digest in seen:
                    dup += 1
                    continue
                cc_dir = (
                    _cc_charges_dir(ctx, message.date)
                    if sender_is_denied(message.sender, cc_senders)
                    else None
                )
                if cc_dir is not None:
                    try:
                        dest = _save(cc_dir, att.name, body, guard=ctx.guard, shadow=ctx.shadow)
                    except (CannotVerify, CopyMismatch) as exc:
                        anomalies.append(_save_anomaly(att.name, exc))
                        continue
                    if ctx.shadow:
                        actions.append(f"would file cc charge {att.name} -> _cc_charges/")
                        continue
                    seen.add(digest)
                    cc_filed += 1
                    actions.append(f"cc charge filed: {dest.name} -> _cc_charges/")
                    events.append(
                        EventSpec(
                            key=f"mailcc:{digest}",
                            event_type=CC_FILED_EVENT,
                            payload={
                                "file": dest.name,
                                "sender_domain": sender_domain(message.sender),
                                "message_date": message.date,
                                "filed_to": str(dest),
                            },
                        )
                    )
                    approvals.append(
                        ApprovalSpec(
                            key=f"mailcc:{digest[:16]}",
                            action_type="expenses.cc_charge_filed",
                            params={
                                "file": dest.name,
                                "sender_domain": sender_domain(message.sender),
                            },
                            reason="credit-card charge receipt filed to _cc_charges/ "
                            "awaiting statement reconciliation; approve to acknowledge",
                        )
                    )
                    continue
                try:
                    dest = _save(landing, att.name, body, guard=ctx.guard, shadow=ctx.shadow)
                except (CannotVerify, CopyMismatch) as exc:
                    anomalies.append(_save_anomaly(att.name, exc))
                    continue
                if ctx.shadow:
                    actions.append(f"would save {att.name} from {sender_domain(message.sender)}")
                    continue
                seen.add(digest)
                saved += 1
                actions.append(f"saved {dest.name} from {sender_domain(message.sender)}")
                events.append(
                    EventSpec(
                        key=f"mailatt:{digest}",
                        event_type=SAVED_EVENT,
                        # sender + sha256 let AP intake bind the invoice to
                        # the mail that delivered it (#356 provenance).
                        payload={
                            "file": dest.name,
                            "sender": sender_address(message.sender),
                            "sender_domain": sender_domain(message.sender),
                            "sha256": digest,
                            "message_date": message.date,
                        },
                    )
                )
    finally:
        _LIVE_CLIENTS.pop(id(ctx), None)

    summary = (
        f"mail: saved {saved}, cc-filed: {cc_filed}, denied: {denied_count}, "
        f"filtered: {filtered}, dup: {dup}, oversize: {oversize}"
    )
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


JOBS: dict[str, JobHandler] = {
    "fetch": JobHandler(key=_fetch_key, run=_fetch_run),
}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {
    "expenses.cc_charge_filed": CardRule("approve", "expense.report", money=False)
}
