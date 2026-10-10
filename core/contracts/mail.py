"""The mail seam: five methods any mailbox adapter provides.

The engine reads a tenant's mailbox for invoices, receipts, and remittance
advices, and writes to it exactly once, the close's statements email behind
an approved card. This is the whole surface; ``core/adapters/graph_mail.py``
(Microsoft Graph) and ``core/adapters/gmail.py`` (Gmail) both fit it, and
``core/adapters/mail.py`` picks one from ``[mail].provider``. Neither is the
default. A job never names a provider.

Shapes only. The listing carries envelopes, never bodies: a body is read
one message at a time, after the caller's own filters have matched, so the
only bodies the engine ever holds are the ones it was looking for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MailSummary:
    """One listed message: identity and envelope, never the body.

    ``received`` is ISO-8601 in UTC with a ``Z`` suffix, whatever the
    provider's own representation, so two adapters list the same mailbox
    the same way.
    """

    id: str
    subject: str
    sender: str
    received: str


@dataclass(frozen=True)
class MailAttachmentRef:
    """A file attached to a message, as the listing describes it. Bytes come
    from :meth:`MailClient.download`."""

    id: str
    name: str
    size: int = 0
    content_type: str = ""


class MailError(RuntimeError):
    """Any mailbox failure the caller may report."""


class MailAuthError(MailError):
    """No usable credential: the owner's consent is missing or expired.
    A caller that must not act without one (the close's send) catches this
    and records that nothing was attempted."""


@runtime_checkable
class MailClient(Protocol):
    """Read a mailbox freely; write it once, behind an approved card."""

    def list_messages(
        self, *, since: str, with_attachments: bool = True, folder: str = "Inbox"
    ) -> list[MailSummary]:
        """Messages received on or after ``since`` (``YYYY-MM-DD``), oldest
        first, across every page. ``with_attachments`` keeps only the
        attachment bearers (the AP feed's rule); the remittance lane passes
        False. ``folder`` is the mail folder to read; an EMPTY folder reads
        the whole mailbox."""
        ...

    def get_body(self, message_id: str) -> tuple[str, str]:
        """``(content_type, content)`` of one message's body: ``"text"`` or
        ``"html"`` and the content as the provider stored it."""
        ...

    def list_attachments(self, message_id: str) -> list[MailAttachmentRef]:
        """File attachments only; forwarded items and references are not files."""
        ...

    def download(self, message_id: str, attachment_id: str) -> bytes: ...

    def send_mail(
        self,
        *,
        subject: str,
        body: str,
        to: Sequence[str],
        attachments: Sequence[tuple[str, str, bytes]] = (),
    ) -> None:
        """The one write. ``attachments`` is ``(filename, content_type, data)``
        triples. Callers MUST hold an approved card; the adapter trusts the
        queue and does not re-check it."""
        ...
