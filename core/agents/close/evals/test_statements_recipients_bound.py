"""Security review 2026-10-03 (#390, LOW): send to the addresses the owner approved.

The statements card names its recipients, but the send read
``[close].statements_recipients`` from the tenant file at send time, so an
edit between approval and the next run mailed the books to an address the
owner never saw on the card. The send now goes to the card's recipients; when
the tenant file has moved on, nothing is sent and a fresh card names the new
addresses. A card from before cards carried recipients keeps the old reading.
"""

from __future__ import annotations

import json

from core.agents.close import jobs as close_jobs
from core.agents.close.evals.test_statements import _card, _job_ctx, _ledger, _run


def _with_recipients(ledger, card_id: int, recipients: str) -> None:
    ledger.conn.execute(
        "UPDATE approval_queue SET params_json = ? WHERE id = ?",
        (json.dumps({"month": "2026-07", "recipients": recipients}), card_id),
    )
    ledger.conn.commit()


def test_the_send_goes_to_the_addresses_on_the_approved_card(tmp_path, monkeypatch):
    with _ledger(tmp_path) as ledger:
        card_id = _card(ledger, status="approved")
        _with_recipients(ledger, card_id, "reviewer@example.com")
        output, mailer = _run(tmp_path, ledger, monkeypatch)
    assert "SENT" in output.summary
    assert [m["to"] for m in mailer.sent] == [["reviewer@example.com"]]


def test_a_tenant_file_edit_after_approval_sends_nothing_and_reasks(tmp_path, monkeypatch):
    with _ledger(tmp_path) as ledger:
        card_id = _card(ledger, status="approved")
        _with_recipients(ledger, card_id, "reviewer@example.com")
        ctx = _job_ctx(tmp_path, ledger, recipients=("someone-else@example.net",))
        output, mailer = _run(tmp_path, ledger, monkeypatch, ctx=ctx)
    assert mailer.sent == []
    assert output.status == "needs_approval"
    assert any(a.code == "close.statements_recipients_changed" for a in output.anomalies)
    (card,) = output.approvals
    assert card.params["recipients"] == "someone-else@example.net"
    assert str(card_id) in card.key
    assert not [e for e in output.events if e.event_type == close_jobs.STATEMENTS_SENT_EVENT]


def test_a_card_without_recipients_keeps_the_old_reading(tmp_path, monkeypatch):
    with _ledger(tmp_path) as ledger:
        _card(ledger, status="approved")  # params carry only the month
        output, mailer = _run(tmp_path, ledger, monkeypatch)
    assert [m["to"] for m in mailer.sent] == [["reviewer@example.com"]]
