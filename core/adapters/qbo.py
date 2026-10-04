"""QBO adapter: read-only evidence of cleared payments.

The accounting system (QuickBooks Online) receives the tenant's bank feed,
so a transaction recorded there IS the cleared-payment evidence the
three-way rule wants (invariant 4). This adapter fetches Purchase and
BillPayment records and normalizes them to :class:`QboEvidence` in integer
cents; downstream matching never sees raw API shapes or floats.

Secrets: the tenant config names a token FILE (outside the repo); this
module reads and rewrites that file. Intuit rotates the refresh token on
every use, so persistence is atomic (write-temp + replace) under an
exclusive lock: losing a rotated token forces a human re-consent.

No SDK: two HTTPS endpoints via stdlib urllib, and the transport is
injectable so tests never open a socket (pinned-dependency policy).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASE = "https://quickbooks.api.intuit.com/v3/company"
# Intuit retired minor versions 1-74 on 2025-08-01; every request below 75 is served as 75 since.
MINOR_VERSION = "75"

Transport = Callable[..., dict]


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iso_since(value: object) -> str:
    """``value`` when it is a real ISO date, else ValueError. Every ``since``
    is interpolated into a query, and some come from model-extracted text
    (security review 2026-10-03, #390)."""
    text = str(value) if value is not None else ""
    if not _ISO_DATE.match(text):
        raise ValueError(f"since {text!r} is not an ISO date (YYYY-MM-DD)")
    from datetime import date

    date.fromisoformat(text)  # 2026-13-01 raises here
    return text


class QboTokenError(RuntimeError):
    """The token file is missing, malformed, or still holds placeholders."""


class QboApiError(RuntimeError):
    """The QBO API refused a request after a valid token exchange."""


class QboEvidence(BaseModel):
    """One cleared money-out transaction, normalized for matching."""

    qbo_id: str  # "<EntityType>:<Id>", unique across entity types
    txn_type: str  # "Purchase" | "BillPayment"
    payee: str = ""
    amount_cents: int
    date: str = ""  # ISO YYYY-MM-DD
    check_ref: str = ""  # check/document number when the payment is a check
    # Raw QBO Bill ids this payment applies to (BillPayment Line/LinkedTxn).
    # The identity that lets reconcile group N partial payments against the
    # one ledger row whose qbo_bill_id matches (issue #115).
    linked_bill_ids: list[str] = []
    # Expense account names this payment codes to (Purchase lines). The hand-
    # check lane's only signal when the registry knows no such payee (phase 7
    # row 7.4): an unregistered contractor has no cost type to read, so the
    # coding is all that separates subcontract labor from an owner loan
    # payoff. Empty for BillPayment, which pays Bills rather than coding
    # expense lines, and is AP by construction.
    accounts: list[str] = []


def _cents(total_amt: Any) -> int:
    """Dollars-and-cents to integer cents through Decimal, never float math."""
    return int((Decimal(str(total_amt)) * 100).to_integral_value())


def _purchase_accounts(obj: dict) -> list[str]:
    """Expense account names on a Purchase's lines, in order, de-duplicated.

    A split Purchase codes to several accounts; the hand-check lane reads
    them all, because one contractor line inside a mixed payment still means
    a person was paid for work.
    """
    names: list[str] = []
    for line in obj.get("Line") or []:
        detail = line.get("AccountBasedExpenseLineDetail") or {}
        name = str((detail.get("AccountRef") or {}).get("name", "") or "")
        if name and name not in names:
            names.append(name)
    return names


def normalize_purchase(obj: dict) -> QboEvidence:
    is_check = str(obj.get("PaymentType", "")) == "Check"
    return QboEvidence(
        qbo_id=f"Purchase:{obj.get('Id', '')}",
        txn_type="Purchase",
        payee=str((obj.get("EntityRef") or {}).get("name", "")),
        amount_cents=_cents(obj.get("TotalAmt", 0)),
        date=str(obj.get("TxnDate", "")),
        check_ref=str(obj.get("DocNumber", "") or "") if is_check else "",
        accounts=_purchase_accounts(obj),
    )


def normalize_billpayment(obj: dict) -> QboEvidence:
    linked: list[str] = []
    for line in obj.get("Line") or []:
        for txn in line.get("LinkedTxn") or []:
            if str(txn.get("TxnType", "")) == "Bill":
                bill_id = str(txn.get("TxnId") or "")  # #314 shape: null -> "", never "None"
                if bill_id and bill_id not in linked:
                    linked.append(bill_id)
    return QboEvidence(
        qbo_id=f"BillPayment:{obj.get('Id', '')}",
        txn_type="BillPayment",
        payee=str((obj.get("VendorRef") or {}).get("name", "")),
        amount_cents=_cents(obj.get("TotalAmt", 0)),
        date=str(obj.get("TxnDate", "")),
        check_ref=str(obj.get("DocNumber", "") or ""),
        linked_bill_ids=linked,
    )


def report_rows(report: dict) -> list[dict]:
    """Flatten a QBO report's nested Rows tree into a list of
    ``{"path": (section titles...), "kind": "data"|"summary", "cells": [...]}``.

    Reports nest sections arbitrarily (Header/Rows/Summary); consumers filter
    by section path or cell content instead of re-walking the raw JSON.
    """
    rows: list[dict] = []

    def _title(node: dict) -> str:
        cols = (node or {}).get("ColData", [])
        return str(cols[0].get("value", "")) if cols else ""

    def walk(container: dict | None, path: tuple[str, ...]) -> None:
        for row in (container or {}).get("Row", []):
            header = row.get("Header")
            title = _title(header) if header else ""
            child_path = path + ((title,) if title else ())
            if row.get("ColData") and not header:
                rows.append(
                    {
                        "path": path,
                        "kind": "data",
                        "cells": [str(c.get("value", "")) for c in row["ColData"]],
                    }
                )
            if row.get("Rows"):
                walk(row["Rows"], child_path)
            summary = row.get("Summary")
            if summary:
                rows.append(
                    {
                        "path": child_path,
                        "kind": "summary",
                        "cells": [str(c.get("value", "")) for c in summary.get("ColData", [])],
                    }
                )

    walk(report.get("Rows"), ())
    return rows


def _default_transport(url: str, *, data: bytes | None, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:  # surface the API's own message
        detail = exc.read().decode()[:300]
        raise QboApiError(f"HTTP {exc.code} from {url.split('?')[0]}: {detail}") from exc


class QboClient:
    """Minimal read-only client bound to one tenant's token file."""

    def __init__(self, token_file: str | Path, *, transport: Transport | None = None) -> None:
        self._path = Path(token_file).expanduser()
        self._transport = transport or _default_transport
        self._access_token: str | None = None
        self._realm: str | None = None

    # ---- token lifecycle ----------------------------------------------------

    def _load(self) -> dict:
        if not self._path.exists():
            raise QboTokenError(f"no QBO token file at {self._path}")
        try:
            tokens = json.loads(self._path.read_text())
        except json.JSONDecodeError as exc:
            raise QboTokenError(f"token file {self._path} is not valid JSON") from exc
        joined = " ".join(str(v) for v in tokens.values())
        if "PASTE" in joined.upper() or "REPLACE" in joined.upper():
            raise QboTokenError(f"token file {self._path} still holds placeholder values")
        return tokens

    def _persist(self, tokens: dict) -> None:
        """Atomic replace so a crash mid-write never truncates the secrets."""
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".qbo-tokens-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(tokens, handle, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def refresh(self) -> str:
        """Exchange the refresh token; persist the ROTATED one before returning.

        Holds an exclusive lock on the token file for the whole exchange so a
        concurrent run cannot interleave a second rotation and orphan ours.
        """
        with open(self._path.parent / ".qbo-tokens.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            tokens = self._load()
            auth = b64encode(f"{tokens['client_id']}:{tokens['client_secret']}".encode()).decode()
            body = urllib.parse.urlencode(
                {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            ).encode()
            response = self._transport(
                TOKEN_URL,
                data=body,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            tokens["refresh_token"] = response["refresh_token"]
            self._persist(tokens)
        self._access_token = response["access_token"]
        self._realm = str(tokens["realm_id"])
        return self._access_token

    # ---- queries -------------------------------------------------------------

    def _query(self, query: str) -> dict:
        if self._access_token is None:
            self.refresh()
        url = f"{API_BASE}/{self._realm}/query?" + urllib.parse.urlencode(
            {"query": query, "minorversion": MINOR_VERSION}
        )
        return self._transport(
            url,
            data=None,
            headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"},
        )

    def _query_pages(self, base: str, entity: str) -> list[dict]:
        """Every row for ``base``, paged past QBO's 1000-row response cap.

        A single ``MAXRESULTS 1000`` query silently truncates row 1001+
        (#136): the duplicate guard and reconcile evidence would lose
        coverage exactly when volume grows. ``base`` carries SELECT/WHERE/
        ORDER BY; this appends STARTPOSITION/MAXRESULTS and loops until a
        short page.
        """
        page_size = 1000
        start = 1
        rows: list[dict] = []
        while True:
            response = self._query(f"{base} STARTPOSITION {start} MAXRESULTS {page_size}")
            batch = (response.get("QueryResponse") or {}).get(entity, [])
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            start += page_size

    def fetch_report(self, name: str, params: dict | None = None) -> dict:
        """One QBO Reports API report (ProfitAndLoss, BalanceSheet,
        AgedPayables, GeneralLedger, ...), raw JSON. Callers walk it with
        :func:`report_rows`."""
        if self._access_token is None:
            self.refresh()
        query = {"minorversion": MINOR_VERSION, **(params or {})}
        url = f"{API_BASE}/{self._realm}/reports/{name}?" + urllib.parse.urlencode(query)
        return self._transport(
            url,
            data=None,
            headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"},
        )

    def fetch_bills_with_balance(self, ids: list[str]) -> dict[str, dict]:
        """{bill_id: {"total_cents": int, "balance_cents": int}} for every id
        QBO still holds. Chunked; an id absent from the result no longer
        exists there."""
        found: dict[str, dict] = {}
        unique = sorted({str(i) for i in ids if str(i)})
        chunk_size = 40  # well inside QBO's query-length limits
        for start in range(0, len(unique), chunk_size):
            chunk = unique[start : start + chunk_size]
            listed = ",".join(f"'{i}'" for i in chunk)
            response = self._query(
                f"SELECT Id, TotalAmt, Balance FROM Bill WHERE Id IN ({listed}) MAXRESULTS 1000"
            )
            for obj in (response.get("QueryResponse") or {}).get("Bill", []):
                found[str(obj.get("Id") or "")] = {  # #314 shape: null -> "", never "None"
                    "total_cents": _cents(obj.get("TotalAmt", 0)),
                    "balance_cents": _cents(obj.get("Balance", 0)),
                }
        return found

    def fetch_journal_entries(self, *, start: str, end: str) -> list[dict]:
        """Raw JournalEntry objects dated within [start, end] (ISO dates)."""
        return self._query_pages(
            f"SELECT * FROM JournalEntry WHERE TxnDate >= '{start}' AND TxnDate <= '{end}' "
            "ORDER BY TxnDate",
            "JournalEntry",
        )

    def fetch_purchases(self, *, start: str, end: str) -> list[dict]:
        """Raw Purchase objects dated within [start, end] (ISO dates), line
        detail included (the owner-transactions check reads account names)."""
        return self._query_pages(
            f"SELECT * FROM Purchase WHERE TxnDate >= '{start}' AND TxnDate <= '{end}' "
            "ORDER BY TxnDate",
            "Purchase",
        )

    def fetch_evidence(self, *, since: str) -> list[QboEvidence]:
        """All money-out transactions dated on/after ``since`` (ISO date)."""
        since = iso_since(since)
        evidence: list[QboEvidence] = []
        for entity, normalize in (
            ("Purchase", normalize_purchase),
            ("BillPayment", normalize_billpayment),
        ):
            for obj in self._query_pages(
                f"SELECT * FROM {entity} WHERE TxnDate >= '{since}' ORDER BY TxnDate",
                entity,
            ):
                evidence.append(normalize(obj))
        return evidence

    # ---- write side (W1: Bills) ---------------------------------------------

    def fetch_vendors(self) -> list[dict]:
        return [
            {"id": str(v["Id"]), "display_name": str(v.get("DisplayName", ""))}
            for v in self._query_pages("SELECT Id, DisplayName FROM Vendor", "Vendor")
        ]

    def fetch_accounts(self) -> list[dict]:
        return [
            {"id": str(a["Id"]), "fully_qualified_name": str(a.get("FullyQualifiedName", ""))}
            for a in self._query_pages("SELECT Id, FullyQualifiedName FROM Account", "Account")
        ]

    def fetch_recent_txns(self, *, since: str) -> list[dict]:
        """Bills AND money-out transactions, normalized for the duplicate
        guard: anything with the same vendor+amount may already record the
        same economic event."""
        since = iso_since(since)
        txns = [
            {
                "vendor": ev.payee,
                "amount_cents": ev.amount_cents,
                "date": ev.date,
                "qbo_id": ev.qbo_id,
            }
            for ev in self.fetch_evidence(since=since)
        ]
        for b in self._query_pages(
            f"SELECT * FROM Bill WHERE TxnDate >= '{since}' ORDER BY TxnDate", "Bill"
        ):
            txns.append(
                {
                    "vendor": str((b.get("VendorRef") or {}).get("name", "")),
                    "amount_cents": _cents(b.get("TotalAmt", 0)),
                    "date": str(b.get("TxnDate", "")),
                    "qbo_id": f"Bill:{b.get('Id', '')}",
                }
            )
        return txns

    def fetch_book_close_date(self) -> str:
        """The tenant's book-closing date (ISO) or empty when books are open.
        Bills dated on/before this are refused by the API (code 6200)."""
        response = self._query("SELECT * FROM Preferences")
        prefs = (response.get("QueryResponse") or {}).get("Preferences", [])
        if not prefs:
            return ""
        acct = prefs[0].get("AccountingInfoPrefs") or {}
        return str(acct.get("BookCloseDate", "") or "")

    def _post(self, entity_path: str, payload: dict) -> dict:
        if self._access_token is None:
            self.refresh()
        url = f"{API_BASE}/{self._realm}/{entity_path}?" + urllib.parse.urlencode(
            {"minorversion": MINOR_VERSION}
        )
        return self._transport(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def create_bill(self, payload: dict) -> dict:
        return (self._post("bill", payload)).get("Bill", {})

    # set_book_close_date REMOVED 2026-08-17 (incident 2026-08-03): the QBO
    # API accepts the Preferences update and silently ignores BookCloseDate —
    # the field is UI-only. The seal is owner-executes-UI + engine-verifies
    # (fetch_book_close_date below is the verification read); a write method
    # here would only pretend to work.

    def get_bill(self, bill_id: str) -> dict:
        if self._access_token is None:
            self.refresh()
        url = f"{API_BASE}/{self._realm}/bill/{bill_id}?" + urllib.parse.urlencode(
            {"minorversion": MINOR_VERSION}
        )
        return (
            self._transport(
                url,
                data=None,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
            )
        ).get("Bill", {})

    def create_purchase(self, payload: dict) -> dict:
        """One expense record (the Purchase entity): the expenses agent's split
        reimbursement write (docs/expenses-design.md §4), under the same
        provenance, duplicate-guard, and readback contract as W1 Bills."""
        return (self._post("purchase", payload)).get("Purchase", {})

    # ---- write side (W2: BillPayments, docs/w2-billpayment-design.md) --------

    def fetch_recent_payments(self, *, since: str) -> list[dict]:
        """Money-out records (Purchase + BillPayment) dated on/after ``since``,
        normalized for W2's duplicate guard and its ``engine:<key>`` lookup:
        same vendor+amount inside the window may already record the check;
        a PrivateNote carrying the engine's key IS the engine's own write
        (a run that died between the create call and its record)."""
        out: list[dict] = []
        for entity, normalize in (
            ("Purchase", normalize_purchase),
            ("BillPayment", normalize_billpayment),
        ):
            for obj in self._query_pages(
                f"SELECT * FROM {entity} WHERE TxnDate >= '{since}' ORDER BY TxnDate",
                entity,
            ):
                ev = normalize(obj)
                out.append(
                    {
                        "qbo_id": ev.qbo_id,
                        "vendor": ev.payee,
                        "amount_cents": ev.amount_cents,
                        "date": ev.date,
                        "check_ref": ev.check_ref,
                        "linked_bill_ids": list(ev.linked_bill_ids),
                        "private_note": str(obj.get("PrivateNote", "") or ""),
                    }
                )
        return out

    def create_bill_payment(self, payload: dict) -> dict:
        """One payment record applying to N Bills (the BillPayment entity),
        under the same provenance, duplicate-guard, and readback contract as
        W1 Bills. A record of a payment the owner already made; never money
        movement (invariant 7)."""
        return (self._post("billpayment", payload)).get("BillPayment", {})

    def get_bill_payment(self, payment_id: str) -> dict:
        return self._get("billpayment", payment_id).get("BillPayment", {})

    def _get(self, entity_path: str, entity_id: str) -> dict:
        if self._access_token is None:
            self.refresh()
        url = f"{API_BASE}/{self._realm}/{entity_path}/{entity_id}?" + urllib.parse.urlencode(
            {"minorversion": MINOR_VERSION}
        )
        return self._transport(
            url,
            data=None,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
        )

    def get_purchase(self, purchase_id: str) -> dict:
        if self._access_token is None:
            self.refresh()
        url = f"{API_BASE}/{self._realm}/purchase/{purchase_id}?" + urllib.parse.urlencode(
            {"minorversion": MINOR_VERSION}
        )
        return (
            self._transport(
                url,
                data=None,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
            )
        ).get("Purchase", {})
