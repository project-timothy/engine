"""qbo-push: the engine records Bills (write side W1).

Split out of ``jobs`` (public issue #2) with its behaviour unchanged; ``jobs``
still owns the ``JOBS`` entry and the QuickBooks client factory. The client
reaches ``run`` as a callable that ``jobs`` resolves at call time, so the evals'
patch of ``jobs._qbo_write_client`` lands here exactly as it did before.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from . import store
from .inputs import vendors


def _push_scope(ctx: JobContext) -> list[dict]:
    """Rows whose Bill belongs in the accounting system: OPEN obligations the
    engine recorded. Settled rows stay out (their money is already in QBO as
    payments; a Bill would double the expense) and so does imported history
    (design default: no backfill)."""
    from .status import is_settled

    rows = ctx.ledger.conn.execute(
        "SELECT * FROM ap_invoices WHERE tenant = ? AND (qbo_bill_id IS NULL "
        "OR qbo_bill_id = '') AND amount_cents > 0 ORDER BY id",
        (ctx.tenant_slug,),
    ).fetchall()
    return [
        dict(r)
        for r in rows
        if not is_settled(str(r["status"]))
        and not str(r["notes"] or "").startswith("[legacy import")
    ]


def _push_approved_row_ids(ctx: JobContext) -> set[int]:
    """Row ids covered by any approved (and not yet exhausted) batch card."""
    covered: set[int] = set()
    cards = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = 'ap.qbo_push_batch' AND status = 'approved'",
        (ctx.tenant_slug,),
    ).fetchall()
    for card in cards:
        ids = json.loads(card["params_json"]).get("row_ids", "")
        covered.update(int(t) for t in str(ids).split(",") if t.strip().isdigit())
    return covered


def run_key(ctx: JobContext) -> str:
    """Re-runs when scope changes OR when an approval lands (the approved
    coverage is part of the input state: same rows + new approval = the run
    that executes)."""
    scope = _push_scope(ctx)
    approved = _push_approved_row_ids(ctx)
    key = RunKey(ctx, "qbopush")
    key.value("scope", [f"{r['id']}:{r['status']}" for r in scope])
    key.value("approved", sorted(str(i) for i in approved))
    # A vendors.toml change alters vendor mapping and so which rows can push;
    # the registry is a run input so an alias fix re-runs instead of
    # replaying the parked outcome (2026-07-20).
    key.vendors(vendors(ctx))
    key.config("qbo")
    return key.digest()


def _bill_payload(row: dict, vendor_id: str, account_id: str, *, book_close: str = "") -> dict:
    """Bill payload. A row dated inside the tenant's closed period posts to
    the first open day (standard bookkeeping; QBO refuses closed-period
    dates, code 6200 — 2026-07-20 incident) with the true invoice date kept
    visible on the record."""
    txn_date = str(row["invoice_date"] or "")
    note = f"engine:{row['idempotency_key']}"
    if txn_date and book_close and txn_date <= book_close:
        from datetime import date, timedelta

        first_open = (date.fromisoformat(book_close) + timedelta(days=1)).isoformat()
        note += f" (invoice dated {txn_date}; posted to first open period)"
        txn_date = first_open
    return {
        "VendorRef": {"value": vendor_id},
        "TxnDate": txn_date or None,
        "DueDate": row["due_date"],
        "DocNumber": str(row["invoice_number"])[:21],
        "PrivateNote": note,
        "Line": [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": round(row["amount_cents"] / 100, 2),
                "Description": str(row["invoice_number"]),
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_id}},
            }
        ],
    }


def earliest_iso_date(rows, *, default: str) -> str:
    """The earliest real ISO ``invoice_date`` among ``rows``, else ``default``.
    The date is model-extracted text and becomes a query bound, so anything
    that is not a date is skipped (security review 2026-10-03, #390)."""
    from ...adapters.qbo import iso_since

    dates = []
    for r in rows:
        try:
            dates.append(iso_since(r["invoice_date"]))
        except ValueError:
            continue
    return min(dates) if dates else default


def _readback_bill(client, bill_id: str, amount_cents: int) -> tuple[bool, str]:
    """Read a just-created Bill back and compare the total. Returns
    (verified, note). Never raises: the Bill exists whatever the readback
    does, and the caller has already remembered its id (honesty audit
    2026-09-03, 02-F1)."""
    expected = round(amount_cents / 100, 2)
    try:
        readback = client.get_bill(bill_id)
    except Exception as exc:  # any transport failure, not only the API's own errors
        return False, f"readback failed: {exc}"
    got = readback.get("TotalAmt")
    try:
        if round(float(got), 2) == expected:
            return True, "readback ok"
    except (TypeError, ValueError):
        pass
    return False, f"readback {got!r}, expected {expected}"


def run(ctx: JobContext, client_factory: Callable[[], Any]) -> JobOutput:
    from .reconcile import DATE_SLACK, _to_date
    from .registry import canonical_vendor as _canon

    scope = _push_scope(ctx)
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []

    if not scope:
        return JobOutput(status="ok", summary="qbo-push: nothing to push")

    if ctx.shadow:
        for row in scope:
            dollars = row["amount_cents"] / 100
            actions.append(
                f"would push {row['vendor']} / {row['invoice_number']} (${dollars:,.2f})"
            )
        return JobOutput(status="ok", summary=f"qbo-push: would push {len(scope)}", actions=actions)

    covered = _push_approved_row_ids(ctx)
    batch = [r for r in scope if r["id"] in covered]

    if not batch:
        # One card for the whole outstanding scope; nothing writes until the
        # owner approves it (invariant 7's spirit for accounting records).
        listing = "; ".join(
            f"#{r['id']} {r['vendor']} {r['invoice_number']} ${r['amount_cents'] / 100:,.2f}"
            for r in scope
        )
        approvals.append(
            ApprovalSpec(
                key=f"qbopush:{hashlib.sha256(listing.encode()).hexdigest()[:16]}",
                action_type="ap.qbo_push_batch",
                params={
                    "count": str(len(scope)),
                    "rows": listing,
                    "row_ids": ",".join(str(r["id"]) for r in scope),
                },
                reason="bills ready to record in the accounting system",
            )
        )
        return JobOutput(
            status="ok",
            summary=f"qbo-push: {len(scope)} awaiting approval",
            approvals=approvals,
        )

    from ...adapters.qbo import QboApiError

    client = client_factory()
    registry = vendors(ctx)
    vendor_map = {_canon(v["display_name"], registry): v["id"] for v in client.fetch_vendors()}
    account_map = {a["fully_qualified_name"]: a["id"] for a in client.fetch_accounts()}
    book_close = client.fetch_book_close_date()
    since = earliest_iso_date(batch, default="2026-01-01")
    recent = client.fetch_recent_txns(since=since)

    pushed = parked = unverified = 0
    for row in batch:
        vendor_token = _canon(str(row["vendor"]), registry)
        vendor_id = vendor_map.get(vendor_token)
        if vendor_id is None:
            parked += 1
            approvals.append(
                ApprovalSpec(
                    key=f"qbomap:v:{row['id']}",
                    action_type="ap.qbo_map_vendor",
                    params={"row_id": str(row["id"]), "vendor": str(row["vendor"])},
                    reason="no accounting-system vendor matches this name; map or create it",
                )
            )
            continue
        account_id = account_map.get(str(row["gl_account"]))
        if account_id is None:
            parked += 1
            approvals.append(
                ApprovalSpec(
                    key=f"qbomap:a:{row['id']}",
                    action_type="ap.qbo_map_account",
                    params={"row_id": str(row["id"]), "gl_account": str(row["gl_account"])},
                    reason="no chart account matches this coding; the chart may have changed",
                )
            )
            continue
        row_date = _to_date(row["invoice_date"])
        lookalike = None
        for txn in recent:
            if _canon(str(txn["vendor"]), registry) != vendor_token:
                continue
            if int(txn["amount_cents"]) != int(row["amount_cents"]):
                continue
            txn_date = _to_date(txn.get("date"))
            if row_date and txn_date and abs(txn_date - row_date) > DATE_SLACK * 3:
                continue
            lookalike = txn
            break
        if lookalike is not None:
            parked += 1
            approvals.append(
                ApprovalSpec(
                    key=f"qbodup:{row['id']}",
                    action_type="ap.qbo_duplicate_review",
                    params={
                        "row_id": str(row["id"]),
                        "vendor": str(row["vendor"]),
                        "invoice_number": str(row["invoice_number"]),
                        "existing": str(lookalike["qbo_id"]),
                    },
                    reason="a similar record already exists; a human decides, never a write",
                )
            )
            continue

        try:
            created = client.create_bill(
                _bill_payload(row, vendor_id, account_id, book_close=book_close)
            )
        except QboApiError as exc:
            # A per-row rejection parks THAT row and the batch keeps writing
            # (2026-07-20 incident: one closed-period date aborted all nine).
            parked += 1
            anomalies.append(
                Anomaly(
                    code="ap.qbo.bill_rejected",
                    detail=f"row #{row['id']} {row['vendor']} / {row['invoice_number']}: {exc}",
                )
            )
            continue
        # #314: .get(key, default) falls back only when the key is
        # ABSENT; an explicit null Id would str() to the truthy "None".
        bill_id = str(created.get("Id") or "")
        label = f"row #{row['id']} {row['vendor']} / {row['invoice_number']}"
        if not bill_id:
            # A create that answered without an Id: nothing to remember and
            # nothing safe to assume. Stop; the lookalike guard is the only
            # barrier on the next run (honesty audit 2026-09-03, 02-F1).
            anomalies.append(
                Anomaly(
                    code="ap.qbo.create_unconfirmed",
                    detail=f"{label}: create returned no Id; batch stopped",
                )
            )
            break
        # Honesty audit 2026-09-03 (02-F1, S1): the Bill exists the instant
        # create returns. Remember it BEFORE the readback, so a readback that
        # raises (a socket timeout is not a QboApiError and used to escape the
        # job with nothing recorded) or disagrees can never leave a Bill the
        # ledger has forgotten and would create again. The row leaves the push
        # scope now; the nightly qbo lens verifies the Bill externally.
        store.record_qbo_ids(ctx.ledger, invoice_id=row["id"], bill_id=bill_id)
        verified, readback_note = _readback_bill(client, bill_id, row["amount_cents"])
        if not verified:
            code = (
                "ap.qbo.readback_failed"
                if readback_note.startswith("readback failed")
                else "ap.qbo.readback_mismatch"
            )
            anomalies.append(
                Anomaly(
                    code=code,
                    detail=f"bill {bill_id} for {label}: {readback_note}; id kept on "
                    "the row, verify in QBO; batch stopped",
                )
            )
        events.append(
            EventSpec(
                key=f"qbobill:{row['idempotency_key']}",
                event_type="ap.qbo.bill_created",
                payload={
                    "invoice_id": row["id"],
                    "vendor": row["vendor"],
                    "invoice_number": row["invoice_number"],
                    "amount_cents": row["amount_cents"],
                    "qbo_bill_id": bill_id,
                    "verified": verified,
                    "readback": readback_note,
                },
            )
        )
        if verified:
            pushed += 1
            actions.append(f"pushed {row['vendor']} / {row['invoice_number']} -> Bill {bill_id}")
            continue
        unverified += 1
        actions.append(
            f"pushed {row['vendor']} / {row['invoice_number']} -> Bill {bill_id} "
            f"(UNVERIFIED: {readback_note})"
        )
        break  # the transport or the book is unhealthy; the rest of the batch waits

    summary = f"qbo-push: pushed {pushed}, parked {parked}"
    if unverified:
        summary = f"qbo-push: pushed {pushed}, unverified {unverified}, parked {parked}"
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )
