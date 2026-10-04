"""Mail-coverage lens evals: arrivals reconcile, privacy holds both ways.
A fake Graph client stands in for the mailbox; no eval opens a socket."""

from __future__ import annotations

from auditor.clients.graph import ListedAttachment, ListedMessage
from auditor.lenses import mail

from .fixtures import add_event, add_run, make_context, make_ledger

FETCH_AT = "2026-07-21T05:00:00+00:00"  # the engine's last look, 1h before NOW
IN_WINDOW = "2026-07-21T01:00:00Z"  # received before the fetch: must be covered
AFTER_FETCH = "2026-07-21T05:30:00Z"  # received after the fetch: tomorrow's work

MAIL_CONFIG = {"denied_senders": ["deniedclinic.example"]}


class FakeGraph:
    def __init__(self, messages):
        self.messages = messages

    def list_messages_with_attachments(self, *, since_iso):
        return self.messages


def _message(sender="billing@vendor.example", date=IN_WINDOW, name="inv.pdf", **attachment):
    kwargs = {"size": 120_000, "is_inline": False}
    kwargs.update(attachment)
    return ListedMessage(
        sender=sender, date=date, attachments=[ListedAttachment(name=name, **kwargs)]
    )


def _world(tmp_path, *, saved_from=()):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="mail", job="fetch", created_at=FETCH_AT)
    for domain, date in saved_from:
        add_event(
            conn,
            event_type="mail.attachment_saved",
            payload={"file": "some.pdf", "sender_domain": domain, "message_date": date},
        )
    return make_context(tmp_path, raw={"mail": MAIL_CONFIG})


def _check(ctx, messages):
    with ctx.ledger:
        return mail.check(ctx, client=FakeGraph(messages))


def test_landed_mail_is_quiet(tmp_path):
    ctx = _world(tmp_path, saved_from=[("vendor.example", IN_WINDOW)])
    assert _check(ctx, [_message()]) == []


def test_unlanded_mail_is_a_finding(tmp_path):
    ctx = _world(tmp_path)
    findings = _check(ctx, [_message()])
    assert [f.condition for f in findings] == ["not-landed"]
    assert "vendor.example" in findings[0].subject


def test_mail_after_the_last_fetch_is_tomorrows_work(tmp_path):
    ctx = _world(tmp_path)
    assert _check(ctx, [_message(date=AFTER_FETCH)]) == []


def test_inline_signature_images_do_not_qualify(tmp_path):
    ctx = _world(tmp_path)
    assert _check(ctx, [_message(name="logo.png", is_inline=True)]) == []


def test_disallowed_extensions_do_not_qualify(tmp_path):
    ctx = _world(tmp_path)
    assert _check(ctx, [_message(name="setup.exe")]) == []


def test_oversize_attachments_do_not_qualify(tmp_path):
    ctx = _world(tmp_path)
    assert _check(ctx, [_message(size=31 * 1024 * 1024)]) == []


def test_denied_mail_is_counted_never_flagged_by_name(tmp_path):
    ctx = _world(tmp_path)
    findings = _check(ctx, [_message(sender="scheduling@deniedclinic.example")])
    assert findings == []  # unlanded is CORRECT for denied mail


def test_saved_event_from_a_denied_domain_is_a_privacy_breach(tmp_path):
    ctx = _world(tmp_path, saved_from=[("deniedclinic.example", IN_WINDOW)])
    findings = _check(ctx, [])
    assert [f.condition for f in findings] == ["privacy-breach"]
    assert findings[0].severity == "CRITICAL"
    # counts and configured domains only; never a file name
    assert "some.pdf" not in findings[0].detail


def test_no_mail_config_is_out_of_scope(tmp_path):
    conn = make_ledger(tmp_path)
    add_run(conn, agent="mail", job="fetch", created_at=FETCH_AT)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert mail.check(ctx, client=FakeGraph([_message()])) == []


def test_no_fetch_run_yet_defers_to_heartbeat(tmp_path):
    make_ledger(tmp_path)
    ctx = make_context(tmp_path, raw={"mail": MAIL_CONFIG})
    with ctx.ledger:
        assert mail.check(ctx, client=FakeGraph([_message()])) == []
