"""Mail agent: normalized message shape and the filter predicates.

The fetch job consumes :class:`MailMessage` records whether they came from
the live Graph adapter or a fixture file; the filters are pure functions so
the privacy-critical denylist behavior is unit-testable in isolation.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Document types the landing folder exists for. Executables, calendar
# invitations, and the like never land.
DEFAULT_ALLOWED_EXTENSIONS = frozenset(
    {"pdf", "xlsx", "xls", "csv", "docx", "doc", "png", "jpg", "jpeg", "heic", "txt"}
)

DEFAULT_MAX_BYTES = 30 * 1024 * 1024  # a document, not a dataset


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
FALLBACK_NAME = "attachment.bin"  # no allowed extension, so it is filtered out


def safe_attachment_name(name: object) -> str:
    """The sender's file name reduced to one path segment: the last
    component after any ``/`` or ``\\``, control characters flattened, no
    leading dot (security review 2026-10-03, finding 6)."""
    last = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    clean = _CONTROL_RE.sub("-", last).strip().lstrip(".").strip()
    return clean or FALLBACK_NAME


class MailAttachment(BaseModel):
    id: str
    name: str
    content_b64: str = ""  # fixtures carry bytes inline; live path downloads

    @field_validator("name", mode="before")
    @classmethod
    def _one_segment(cls, v: object) -> str:
        return safe_attachment_name(v)


class MailMessage(BaseModel):
    id: str
    sender: str = ""
    date: str = ""  # ISO timestamp from the mailbox
    attachments: list[MailAttachment] = Field(default_factory=list)


def sender_is_denied(sender: str, denied: list[str]) -> bool:
    """True when the sender matches any denylist entry (address or domain
    substring, case-insensitive). Deny on match — a personal document in a
    business archive is worse than a business document arriving late."""
    s = sender.lower().strip()
    return any(token.lower().strip() in s for token in denied if token.strip())


def extension_allowed(filename: str, allowed: frozenset[str] | set[str]) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in allowed


def sender_address(sender: str) -> str:
    """The bare, lower-cased address: ``"Name <a@b.com>"`` -> ``"a@b.com"``."""
    s = sender.strip()
    if "<" in s and s.endswith(">"):
        s = s.rsplit("<", 1)[1][:-1]
    return s.strip().lower()


def sender_domain(sender: str) -> str:
    address = sender_address(sender)
    return address.rsplit("@", 1)[-1] if "@" in address else ""
