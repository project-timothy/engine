"""QBO adapter: token lifecycle and evidence normalization.

The adapter is the engine's read-only line into the accounting system of
record. Two hard requirements shape these tests:

- Intuit ROTATES the refresh token on every use; losing the rotated value
  bricks the connection until a human re-consents. Persistence is therefore
  atomic and tested.
- Everything downstream consumes normalized :class:`QboEvidence` records in
  integer cents; no float ever touches a money value (invariant 2's sibling:
  code computes, and it computes exactly).

No test opens a network connection: the client takes an injectable transport.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from core.adapters.qbo import (
    QboClient,
    QboTokenError,
    normalize_billpayment,
    normalize_purchase,
)

TOKENS = {
    "client_id": "cid-123",
    "client_secret": "sec-456",
    "refresh_token": "rt-OLD",
    "realm_id": "999000111",
}


def _token_file(tmp_path: Path) -> Path:
    p = tmp_path / "qbo-tokens.json"
    p.write_text(json.dumps(TOKENS))
    p.chmod(0o600)
    return p


def _fake_transport(responses: list[dict]):
    """A transport that replays canned JSON bodies and records requests."""
    calls: list[dict] = []

    def transport(url: str, *, data: bytes | None, headers: dict) -> dict:
        calls.append({"url": url, "data": data, "headers": headers})
        return responses[len(calls) - 1]

    transport.calls = calls
    return transport


# ---------- token lifecycle --------------------------------------------------


def test_refresh_rotates_and_persists_the_new_token(tmp_path):
    p = _token_file(tmp_path)
    transport = _fake_transport(
        [{"access_token": "at-1", "refresh_token": "rt-NEW", "expires_in": 3600}]
    )
    client = QboClient(p, transport=transport)

    access = client.refresh()

    assert access == "at-1"
    stored = json.loads(p.read_text())
    assert stored["refresh_token"] == "rt-NEW"  # rotated value survives the process
    assert stored["client_secret"] == "sec-456"  # everything else untouched
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600  # secrets file stays owner-only


def test_refresh_refuses_a_placeholder_token_file(tmp_path):
    p = tmp_path / "qbo-tokens.json"
    p.write_text(json.dumps({**TOKENS, "refresh_token": "PASTE_REFRESH_TOKEN_HERE"}))
    with pytest.raises(QboTokenError):
        QboClient(p, transport=_fake_transport([])).refresh()


def test_missing_token_file_is_a_clear_error(tmp_path):
    with pytest.raises(QboTokenError):
        QboClient(tmp_path / "absent.json", transport=_fake_transport([])).refresh()


# ---------- evidence normalization -------------------------------------------


def test_purchase_check_normalizes_to_cents_and_check_ref():
    ev = normalize_purchase(
        {
            "Id": "201",
            "PaymentType": "Check",
            "DocNumber": "9051",
            "TxnDate": "2026-07-14",
            "TotalAmt": 2300.00,
            "EntityRef": {"name": "Acme Tooling", "value": "55"},
        }
    )
    assert ev.qbo_id == "Purchase:201"
    assert ev.payee == "Acme Tooling"
    assert ev.amount_cents == 230000
    assert ev.check_ref == "9051"
    assert ev.date == "2026-07-14"


def test_billpayment_normalizes_vendor_and_amount():
    ev = normalize_billpayment(
        {
            "Id": "301",
            "PayType": "Check",
            "DocNumber": "1088",
            "TxnDate": "2026-07-15",
            "TotalAmt": 78.1,
            "VendorRef": {"name": "Beta Freight", "value": "56"},
        }
    )
    assert ev.qbo_id == "BillPayment:301"
    assert ev.payee == "Beta Freight"
    assert ev.amount_cents == 7810  # Decimal path: 78.1 dollars is exactly 7810 cents
    assert ev.check_ref == "1088"
    assert ev.linked_bill_ids == []


def test_billpayment_carries_linked_bill_ids():
    """A partial payment matched against a bill in QBO links the bill via
    Line/LinkedTxn; the id is the identity that lets reconcile group N
    partials against one row (issue #115). Non-Bill links are ignored and
    duplicates fold."""
    ev = normalize_billpayment(
        {
            "Id": "269",
            "DocNumber": "9056",
            "TxnDate": "2026-08-05",
            "TotalAmt": 10000.00,
            "VendorRef": {"name": "Acme Tooling", "value": "55"},
            "Line": [
                {
                    "Amount": 10000.00,
                    "LinkedTxn": [
                        {"TxnId": "777", "TxnType": "Bill"},
                        {"TxnId": "777", "TxnType": "Bill"},
                        {"TxnId": "888", "TxnType": "Deposit"},
                    ],
                },
                {"Amount": 0, "LinkedTxn": []},
            ],
        }
    )
    assert ev.linked_bill_ids == ["777"]


def test_card_purchase_has_no_check_ref():
    ev = normalize_purchase(
        {
            "Id": "205",
            "PaymentType": "CreditCard",
            "TxnDate": "2026-07-14",
            "TotalAmt": 42.50,
            "EntityRef": {"name": "Roadside Coffee", "value": "77"},
        }
    )
    assert ev.check_ref == ""
    assert ev.amount_cents == 4250


def test_purchase_without_entity_still_normalizes():
    ev = normalize_purchase(
        {
            "Id": "206",
            "PaymentType": "Check",
            "DocNumber": "88",
            "TxnDate": "2026-07-14",
            "TotalAmt": 10,
        }
    )
    assert ev.payee == ""
    assert ev.amount_cents == 1000


# ---------- pagination (#136) ------------------------------------------------

REFRESH = {"access_token": "at-1", "refresh_token": "rt-NEW", "expires_in": 3600}


def _purchases(n, offset=0):
    return [
        {
            "Id": str(offset + i),
            "PaymentType": "CreditCard",
            "TxnDate": "2026-07-01",
            "TotalAmt": 1.00,
        }
        for i in range(1, n + 1)
    ]


def test_fetch_purchases_pages_past_1000_rows(tmp_path):
    """QBO caps a query response at 1000 rows. A single MAXRESULTS 1000
    query silently loses row 1001+ — the duplicate guard and reconcile
    evidence lose coverage exactly when volume grows (#136)."""
    p = _token_file(tmp_path)
    transport = _fake_transport(
        [
            REFRESH,
            {"QueryResponse": {"Purchase": _purchases(1000)}},
            {"QueryResponse": {"Purchase": _purchases(3, offset=1000)}},
        ]
    )
    client = QboClient(p, transport=transport)

    rows = client.fetch_purchases(start="2026-07-01", end="2026-07-31")

    assert len(rows) == 1003
    queries = [c["url"] for c in transport.calls[1:]]
    assert "STARTPOSITION+1+" in queries[0] or "STARTPOSITION%201%20" in queries[0]
    assert "STARTPOSITION+1001+" in queries[1] or "STARTPOSITION%201001%20" in queries[1]


def test_short_page_stops_after_one_query(tmp_path):
    p = _token_file(tmp_path)
    transport = _fake_transport(
        [REFRESH, {"QueryResponse": {"Vendor": [{"Id": "1", "DisplayName": "V"}]}}]
    )
    client = QboClient(p, transport=transport)

    vendors = client.fetch_vendors()

    assert len(vendors) == 1
    assert len(transport.calls) == 2  # refresh + exactly one page


def test_fetch_evidence_pages_both_entities(tmp_path):
    p = _token_file(tmp_path)
    transport = _fake_transport(
        [
            REFRESH,
            {"QueryResponse": {"Purchase": _purchases(1000)}},
            {"QueryResponse": {"Purchase": _purchases(2, offset=1000)}},
            {"QueryResponse": {"BillPayment": []}},
        ]
    )
    client = QboClient(p, transport=transport)

    evidence = client.fetch_evidence(since="2026-07-01")

    assert len(evidence) == 1002
