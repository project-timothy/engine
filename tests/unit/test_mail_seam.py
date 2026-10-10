"""The mail seam (extraction plan gate 3): one fixture set, two adapters.

Both adapters are driven through fake transports over the SAME mailbox, and
every method must answer identically, because that is what makes the seam
real rather than nominal. The factory picks the adapter from
``[mail].provider`` and refuses an empty one: the engine ships no default.
No job may construct an adapter by name; the contracts package may import
nothing from the rest of ``core``.
"""

from __future__ import annotations

import ast
import base64
import email
import json
import urllib.parse
from pathlib import Path

import pytest

from core.adapters import mail as mail_factory
from core.adapters.gmail import GmailClient, GmailError, _b64url_encode
from core.adapters.graph_mail import GraphMailClient, GraphMailError
from core.contracts.mail import MailAttachmentRef, MailAuthError, MailClient, MailSummary
from core.engine.config import MailSettings

REPO = Path(__file__).resolve().parents[2]

# ---- one mailbox, described twice --------------------------------------------

EXPECTED = [
    MailSummary(
        id="msg-3",
        subject="Statement",
        sender="ar@betafreight.com",
        received="2026-07-14T09:00:00Z",
    ),
    MailSummary(
        id="msg-1",
        subject="Invoice 4471",
        sender="billing@acmetooling.com",
        received="2026-07-15T12:00:00Z",
    ),
]
NO_ATTACHMENT = MailSummary(
    id="msg-2", subject="Newsletter", sender="news@example.org", received="2026-07-15T13:00:00Z"
)
EXPECTED_ATTACHMENTS = [
    MailAttachmentRef(
        id="att-1", name="invoice-4471.pdf", size=12345, content_type="application/pdf"
    )
]

GRAPH_PAGE_1 = {
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
GRAPH_PAGE_2 = {
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
GRAPH_ATTACHMENTS = {
    "value": [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "att-1",
            "name": "invoice-4471.pdf",
            "size": 12345,
            "contentType": "application/pdf",
        },
        {"@odata.type": "#microsoft.graph.itemAttachment", "id": "att-2", "name": "fwd"},
    ]
}


def _ms(iso: str) -> str:
    from datetime import UTC, datetime

    return str(
        int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() * 1000)
    )


GMAIL_META = {
    "msg-1": {
        "id": "msg-1",
        "internalDate": _ms("2026-07-15T12:00:00Z"),
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Invoice 4471"},
                {"name": "From", "value": "Acme Tooling <billing@acmetooling.com>"},
            ]
        },
    },
    "msg-2": {
        "id": "msg-2",
        "internalDate": _ms("2026-07-15T13:00:00Z"),
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Newsletter"},
                {"name": "From", "value": "news@example.org"},
            ]
        },
    },
    "msg-3": {
        "id": "msg-3",
        "internalDate": _ms("2026-07-14T09:00:00Z"),
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Statement"},
                {"name": "From", "value": "ar@betafreight.com"},
            ]
        },
    },
}
GMAIL_FULL_MSG_1 = {
    "id": "msg-1",
    "payload": {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url_encode(b"paid")}},
                    {"mimeType": "text/html", "body": {"data": _b64url_encode(b"<p>paid</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice-4471.pdf",
                "body": {"attachmentId": "att-1", "size": 12345},
            },
        ],
    },
}


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.posts: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, *, data=None, headers=None):
        self.calls.append(url)
        if data is not None:
            self.posts.append((url, data, headers))
            return b""
        if "next-page" in url:
            return GRAPH_PAGE_2
        if "/messages?" in url:
            return GRAPH_PAGE_1
        if url.endswith("/attachments/att-1/$value"):
            return b"pdf-bytes"
        if url.endswith("/attachments"):
            return GRAPH_ATTACHMENTS
        if "$select=body" in url:
            return {"body": {"contentType": "html", "content": "<p>paid</p>"}}
        raise AssertionError(f"unexpected Graph call {url}")


class FakeGmail:
    """Serves the same mailbox in Gmail's shapes, newest first, two pages,
    and honours ``has:attachment`` in the query the way Gmail does."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.posts: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, *, data=None, headers=None):
        self.calls.append(url)
        if data is not None:
            self.posts.append((url, data, headers))
            return {"id": "sent-1"}
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/messages"):
            q = query.get("q", [""])[0]
            ids = ["msg-2", "msg-1", "msg-3"]
            if "has:attachment" in q:
                ids = [i for i in ids if i != "msg-2"]
            if query.get("pageToken") == ["p2"]:
                return {"messages": [{"id": ids[-1]}]}
            return {"messages": [{"id": i} for i in ids[:-1]], "nextPageToken": "p2"}
        if "/attachments/att-1" in parsed.path:
            return {"data": _b64url_encode(b"pdf-bytes"), "size": 9}
        message_id = parsed.path.rsplit("/", 1)[1]
        if query.get("format") == ["metadata"]:
            return GMAIL_META[message_id]
        if query.get("format") == ["full"]:
            return GMAIL_FULL_MSG_1
        raise AssertionError(f"unexpected Gmail call {url}")


def _graph() -> tuple[GraphMailClient, FakeGraph]:
    fake = FakeGraph()
    return GraphMailClient(token_provider=lambda: "tok", transport=fake), fake


def _gmail() -> tuple[GmailClient, FakeGmail]:
    fake = FakeGmail()
    return GmailClient(token_provider=lambda: "tok", transport=fake), fake


ADAPTERS = [("graph", _graph), ("gmail", _gmail)]


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_adapters_satisfy_the_seam(name, build):
    client, _ = build()
    assert isinstance(client, MailClient), name


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_list_the_same_mailbox_oldest_first_across_pages(name, build):
    client, _ = build()
    assert client.list_messages(since="2026-07-13") == EXPECTED, name


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_keep_the_bodiless_advice_when_asked(name, build):
    client, _ = build()
    listing = client.list_messages(since="2026-07-13", with_attachments=False)
    assert listing == [EXPECTED[0], EXPECTED[1], NO_ATTACHMENT], name


def test_an_empty_folder_reads_the_whole_mailbox_in_both_dialects():
    graph, fake_graph = _graph()
    graph.list_messages(since="2026-07-13", folder="")
    assert "/me/messages?" in fake_graph.calls[0] and "mailFolders" not in fake_graph.calls[0]
    everywhere, fake_everywhere = _gmail()
    everywhere.list_messages(since="2026-07-13", folder="")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake_everywhere.calls[0]).query)["q"][0]
    assert "in:" not in q and "label:" not in q
    inbox, fake_inbox = _gmail()
    inbox.list_messages(since="2026-07-13")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake_inbox.calls[0]).query)["q"][0]
    assert "in:inbox" in q
    assert GmailClient.query(since="2026-07-13", with_attachments=False, folder="Remittances") == (
        "after:2026/07/13 label:Remittances"
    )


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_list_the_same_file_attachments(name, build):
    client, _ = build()
    assert client.list_attachments("msg-1") == EXPECTED_ATTACHMENTS, name


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_read_one_body_the_same_way(name, build):
    client, _ = build()
    content_type, content = client.get_body("msg-1")
    assert content_type in ("text", "html"), name
    assert "paid" in content, name


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_download_the_same_bytes(name, build):
    client, _ = build()
    assert client.download("msg-1", "att-1") == b"pdf-bytes", name


@pytest.mark.parametrize("name,build", ADAPTERS)
def test_both_refuse_a_send_with_no_recipients(name, build):
    client, _ = build()
    with pytest.raises((GraphMailError, GmailError), match="no recipients"):
        client.send_mail(subject="s", body="b", to=[])


def test_both_sends_carry_the_recipient_subject_and_attachment():
    graph, fake_graph = _graph()
    graph.send_mail(
        subject="Statements",
        body="Attached.",
        to=["owner@example.com"],
        attachments=[("statements.xlsx", "application/x-test", b"workbook-bytes")],
    )
    url, data, _ = fake_graph.posts[0]
    payload = json.loads(data)["message"]
    assert url.endswith("/me/sendMail")
    assert payload["toRecipients"] == [{"emailAddress": {"address": "owner@example.com"}}]
    assert base64.b64decode(payload["attachments"][0]["contentBytes"]) == b"workbook-bytes"

    gmail, fake_gmail = _gmail()
    gmail.send_mail(
        subject="Statements",
        body="Attached.",
        to=["owner@example.com"],
        attachments=[("statements.xlsx", "application/x-test", b"workbook-bytes")],
    )
    url, data, headers = fake_gmail.posts[0]
    assert url.endswith("/messages/send")
    assert headers["Content-Type"] == "application/json"
    raw = json.loads(data)["raw"]
    mime = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert mime["To"] == "owner@example.com"
    assert mime["Subject"] == "Statements"
    parts = list(mime.walk())
    attached = [p for p in parts if p.get_filename() == "statements.xlsx"]
    assert attached and attached[0].get_payload(decode=True) == b"workbook-bytes"


# ---- the factory ---------------------------------------------------------------


def _settings(**overrides) -> MailSettings:
    base = dict(
        client_id="client-1",
        tenant_id="tenant-1",
        keychain_service="svc",
        keychain_account="owner@example.test",
    )
    base.update(overrides)
    return MailSettings(**base)


def test_an_empty_provider_is_refused_by_name():
    with pytest.raises(mail_factory.MailProviderError, match=r"\[mail\]\.provider is empty"):
        mail_factory.client_for(_settings(provider=""))


def test_an_unknown_provider_is_refused_by_name():
    with pytest.raises(mail_factory.MailProviderError, match="carrier-pigeon"):
        mail_factory.client_for(_settings(provider="carrier-pigeon"))


def test_graph_builds_the_graph_client_without_touching_the_keychain():
    client = mail_factory.client_for(_settings(provider="graph"))
    assert isinstance(client, GraphMailClient)


def test_gmail_needs_its_client_secret_named_and_set(monkeypatch):
    monkeypatch.delenv("MAILBOX_SECRET", raising=False)
    with pytest.raises(MailAuthError, match="client_secret_env"):
        mail_factory.client_for(_settings(provider="gmail"))
    with pytest.raises(MailAuthError, match="MAILBOX_SECRET is not set"):
        mail_factory.client_for(_settings(provider="gmail", client_secret_env="MAILBOX_SECRET"))
    monkeypatch.setenv("MAILBOX_SECRET", "s3")
    client = mail_factory.client_for(
        _settings(provider="gmail", client_secret_env="MAILBOX_SECRET")
    )
    assert isinstance(client, GmailClient)


def test_eager_acquires_the_token_before_handing_the_client_back(monkeypatch):
    acquired: list[str] = []

    def fake_provider(**names):
        def provider():
            acquired.append("graph")
            return "tok"

        return provider

    import core.adapters.graph_mail as graph_mail

    monkeypatch.setattr(graph_mail, "keychain_token_provider", fake_provider)
    client = mail_factory.client_for(_settings(provider="graph"), eager=True)
    assert acquired == ["graph"], "the token was acquired inside the factory"
    assert isinstance(client, GraphMailClient)


def test_a_missing_consent_surfaces_as_the_seams_auth_error(monkeypatch):
    def fake_provider(**names):
        def provider():
            from core.adapters.graph_mail import GraphAuthError

            raise GraphAuthError("no MSAL token cache")

        return provider

    import core.adapters.graph_mail as graph_mail

    monkeypatch.setattr(graph_mail, "keychain_token_provider", fake_provider)
    with pytest.raises(MailAuthError):
        mail_factory.client_for(_settings(provider="graph"), eager=True)


# ---- the boundaries ------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


# The calendar is its own seam (``detect_provider`` picks it), and its only
# writer is Graph's: it borrows the Graph keychain token and Graph's errors,
# never a mail client.
NOT_MAIL = {"core/agents/deadlines/calendar_sync.py"}


def test_no_job_names_a_mail_provider():
    """Every job goes through the factory; the seam is real only if nothing
    reaches around it."""
    offenders = []
    for path in (REPO / "core" / "agents").rglob("*.py"):
        if "evals" in path.parts or str(path.relative_to(REPO)) in NOT_MAIL:
            continue
        for name in _imports(path):
            if name.endswith(("graph_mail", "gmail")) or "graph_mail" in name or ".gmail" in name:
                offenders.append(f"{path.relative_to(REPO)}: {name}")
    assert offenders == [], offenders
