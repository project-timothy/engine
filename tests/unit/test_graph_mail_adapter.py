"""Graph mail adapter: listing, filtering, download, and the one gated send.

The adapter is the engine's line into the tenant's mailbox. Tests inject
both the transport and the token provider, so nothing here touches msal,
keyring, or a socket.
"""

from __future__ import annotations

import base64
import json

import pytest

from core.adapters.graph_mail import GraphMailClient, GraphMailError

PAGE_1 = {
    "value": [
        {
            "id": "msg-1",
            "subject": "Invoice 4471",
            "receivedDateTime": "2026-07-15T12:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"address": "billing@acmetooling.com"}},
        },
        {
            "id": "msg-2",
            "subject": "Newsletter",
            "receivedDateTime": "2026-07-15T13:00:00Z",
            "hasAttachments": False,
            "from": {"emailAddress": {"address": "news@example.org"}},
        },
    ],
    "@odata.nextLink": "https://graph.example/next-page",
}
PAGE_2 = {
    "value": [
        {
            "id": "msg-3",
            "subject": "Statement",
            "receivedDateTime": "2026-07-14T09:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"address": "ar@betafreight.com"}},
        }
    ]
}
ATTACHMENTS = {
    "value": [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "att-1",
            "name": "invoice-4471.pdf",
            "size": 12345,
        },
        {
            "@odata.type": "#microsoft.graph.itemAttachment",
            "id": "att-2",
            "name": "forwarded message",
        },
    ]
}


def _client(responses):
    calls = []

    def transport(url, *, data=None, headers=None):
        calls.append(url)
        return responses[len(calls) - 1]

    client = GraphMailClient(token_provider=lambda: "tok-1", transport=transport)
    client.calls = calls
    return client


def test_list_messages_follows_pagination_and_keeps_only_attachment_bearers():
    client = _client([PAGE_1, PAGE_2])
    messages = client.list_messages(since="2026-07-13")
    assert [m["id"] for m in messages] == ["msg-1", "msg-3"]  # msg-2 has no attachments
    assert len(client.calls) == 2  # followed @odata.nextLink


def test_list_attachments_returns_file_attachments_only():
    client = _client([ATTACHMENTS])
    atts = client.list_attachments("msg-1")
    assert [a["id"] for a in atts] == ["att-1"]


def test_download_returns_raw_bytes():
    client = _client([b"pdf-bytes"])  # the /$value endpoint returns the file itself
    assert client.download("msg-1", "att-1") == b"pdf-bytes"
    assert client.calls[0].endswith("/attachments/att-1/$value")


def test_send_mail_posts_recipients_and_base64_attachment():
    sent = []

    def transport(url, *, data=None, headers=None):
        sent.append((url, data, headers))
        return b""  # Graph answers 202 with an empty body

    client = GraphMailClient(token_provider=lambda: "tok-1", transport=transport)
    client.send_mail(
        subject="Statements",
        body="Attached.",
        to=["pat@example.com"],
        attachments=[("statements.xlsx", "application/x-test", b"workbook-bytes")],
    )
    url, data, headers = sent[0]
    assert url.endswith("/me/sendMail")
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(data)
    message = payload["message"]
    assert payload["saveToSentItems"] is True
    assert message["toRecipients"] == [{"emailAddress": {"address": "pat@example.com"}}]
    attachment = message["attachments"][0]
    assert attachment["name"] == "statements.xlsx"
    assert base64.b64decode(attachment["contentBytes"]) == b"workbook-bytes"


def test_send_mail_refuses_empty_recipients():
    client = GraphMailClient(token_provider=lambda: "tok-1", transport=lambda *a, **k: b"")
    with pytest.raises(GraphMailError, match="no recipients"):
        client.send_mail(subject="s", body="b", to=[])


# ---- body-only mail (the AR remittance lane, issue #282) ---------------------


def test_list_messages_can_keep_the_ones_with_no_attachment():
    """A remittance advice carries no attachment: its numbers are in the
    body. The attachment filter is the AP feed's rule, not the mailbox's."""
    client = _client([PAGE_1, PAGE_2])
    messages = client.list_messages(since="2026-07-13", with_attachments=False)
    assert [m["id"] for m in messages] == ["msg-1", "msg-2", "msg-3"]


def test_list_messages_reads_the_inbox_by_default_and_the_whole_mailbox_when_asked():
    """A remittance advice is routinely filed into a folder (or deleted)
    before the next morning's run, so the AR lane reads every folder. The AP
    feed's inbox scope is unchanged."""
    inbox = _client([PAGE_2])
    inbox.list_messages(since="2026-07-13")
    assert "/me/mailFolders/Inbox/messages?" in inbox.calls[0]

    everywhere = _client([PAGE_2])
    everywhere.list_messages(since="2026-07-13", folder="")
    assert "/me/messages?" in everywhere.calls[0]
    assert "mailFolders" not in everywhere.calls[0]


def test_get_body_returns_the_content_type_and_the_content():
    """One message, one read, body only: the adapter never pulls bodies in
    the listing, so only a message the caller already matched is opened."""
    client = _client([{"body": {"contentType": "html", "content": "<p>paid</p>"}}])
    content_type, content = client.get_body("msg-1")
    assert (content_type, content) == ("html", "<p>paid</p>")
    assert client.calls[0].endswith("/me/messages/msg-1?$select=body")
