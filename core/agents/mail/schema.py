"""Mail agent: normalized message shape and the filter predicates.

The fetch job consumes :class:`MailMessage` records whether they came from
the live Graph adapter or a fixture file; the filters are pure functions so
the privacy-critical denylist behavior is unit-testable in isolation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Document types the landing folder exists for. Executables, calendar
# invitations, and the like never land.
DEFAULT_ALLOWED_EXTENSIONS = frozenset(
    {"pdf", "xlsx", "xls", "csv", "docx", "doc", "png", "jpg", "jpeg", "heic", "txt"}
)

DEFAULT_MAX_BYTES = 30 * 1024 * 1024  # a document, not a dataset


class MailAttachment(BaseModel):
    id: str
    name: str
    content_b64: str = ""  # fixtures carry bytes inline; live path downloads


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


def sender_domain(sender: str) -> str:
    return sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
