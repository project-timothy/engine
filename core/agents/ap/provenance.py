"""Sender provenance: does the mail that delivered an invoice belong to the
vendor the invoice names? (issue #356, shadow stage)

Intake binds an invoice to a vendor by the name printed in the PDF. Any
sender whose PDF names a known vendor files as that vendor, so the name alone
proves nothing. The mail fetch records who sent every saved attachment
(``mail.attachment_saved``, keyed by the content's sha256); this module joins
an invoice to that record and states a verdict.

Verdicts, strongest binding first:

- ``match``: the sender is the vendor's own domain (a domain-shaped
  vendors.toml key, exactly or as a subdomain) or an exact address listed in
  the entry's ``senders``.
- ``internal_forward``: someone at the tenant forwarded it. The forwarder
  vouches for it the way an owner drop does.
- ``owner_drop``: no mail record; a person put the file in the folder.
- ``platform``: an invoicing platform's shared mailer (tenant config). Anyone
  with an account on the platform can send from it under any company name,
  so it binds nothing; later checks (first invoice, remit change) carry it.
- ``mismatch``: none of the above. ``reason`` says which kind: ``domain``
  (a business domain that is not the vendor's) or ``freemail_unlisted`` (a
  free-mail address not written into the vendor's ``senders``).

Shadow stage: the verdict is recorded as an event and counted by the
auditor; it places no hold and changes no invoice. Enforcement waits for two
weeks of verdicts (false-hold count) and the owner's go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import VendorRegistry

PROVENANCE_EVENT = "ap.provenance.recorded"
MAIL_SAVED_EVENT = "mail.attachment_saved"

MATCH = "match"
INTERNAL_FORWARD = "internal_forward"
OWNER_DROP = "owner_drop"
PLATFORM = "platform"
MISMATCH = "mismatch"


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reason: str = ""


@dataclass(frozen=True)
class MailOrigin:
    sender: str  # full address when the fetch recorded it, else ""
    domain: str


def _under(domain: str, parent: str) -> bool:
    parent = parent.strip().lower()
    return bool(parent) and (domain == parent or domain.endswith("." + parent))


def _vendor_domains(registry: VendorRegistry, vendor: str) -> list[str]:
    """The vendor's domain-shaped registry keys. A bare name fragment
    ("acme") is a legacy subject-matching key, never proof of a sender: a
    look-alike domain can contain any fragment."""
    low = vendor.strip().lower()
    return [
        key.lower()
        for key, entry in registry.entries.items()
        if entry.vendor.lower() == low and "." in key
    ]


def _vendor_senders(registry: VendorRegistry, vendor: str) -> set[str]:
    low = vendor.strip().lower()
    return {
        s.strip().lower()
        for entry in registry.entries.values()
        if entry.vendor.lower() == low
        for s in entry.senders
    }


def judge(
    origin: MailOrigin | None,
    vendor: str,
    registry: VendorRegistry,
    *,
    internal_domains: list[str],
    platform_domains: list[str],
    freemail_domains: list[str],
) -> Verdict:
    if origin is None:
        return Verdict(OWNER_DROP)
    domain = origin.domain.lower()
    if origin.sender and origin.sender.lower() in _vendor_senders(registry, vendor):
        return Verdict(MATCH, "listed sender")
    if any(_under(domain, d) for d in internal_domains):
        return Verdict(INTERNAL_FORWARD)
    if any(_under(domain, d) for d in platform_domains):
        return Verdict(PLATFORM)
    if any(_under(domain, d) for d in freemail_domains):
        return Verdict(MISMATCH, "freemail_unlisted")
    if any(_under(domain, d) for d in _vendor_domains(registry, vendor)):
        return Verdict(MATCH, "vendor domain")
    return Verdict(MISMATCH, "domain")


def mail_origins(
    events: list[dict[str, Any]],
) -> tuple[dict[str, MailOrigin], dict[str, MailOrigin]]:
    """Index every saved attachment by content sha256 and by file name.

    The sha256 rides in the event key (``...mailatt:<sha256>``) for every
    record, and in the payload since #356; the full sender address only
    since #356, so older records bind by domain alone. The file-name index is
    the fallback for a file renamed by nothing (the fetch never overwrites, so
    a landed name is unique at save time); the newest record wins.
    """
    by_hash: dict[str, MailOrigin] = {}
    by_name: dict[str, MailOrigin] = {}
    for event in events:
        if event.get("event_type") != MAIL_SAVED_EVENT:
            continue
        payload = event.get("payload") or {}
        origin = MailOrigin(
            sender=str(payload.get("sender") or ""),
            domain=str(payload.get("sender_domain") or ""),
        )
        digest = str(payload.get("sha256") or "")
        if not digest:
            key = str(event.get("idempotency_key") or "")
            if "mailatt:" in key:
                digest = key.rsplit("mailatt:", 1)[1]
        if digest:
            by_hash[digest] = origin
        name = str(payload.get("file") or "")
        if name:
            by_name[name] = origin
    return by_hash, by_name
