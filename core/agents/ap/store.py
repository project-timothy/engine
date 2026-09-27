"""AP persistence over the ledger's SQLite connection.

The invoice identity key is ``vendor + invoice_number`` (never amount); the
idempotency key on ap_invoices is derived from it, so a duplicate arrival can
never double-insert no matter what the classifier concludes. Status flips go
through :func:`update_status`, which enforces the transition rules and writes
the paired history row (one row per flip, the AuditTrail discipline).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ...ledger import Ledger
from .status import assert_transition


def _now() -> str:
    return datetime.now(UTC).isoformat()


def invoice_key(tenant: str, vendor: str, invoice_number: str) -> str:
    return f"apinv:{tenant}:{vendor.strip().lower()}:{invoice_number.strip().lower()}"


def find_invoice(ledger: Ledger, tenant: str, vendor: str, invoice_number: str) -> Any | None:
    row = ledger.conn.execute(
        "SELECT * FROM ap_invoices WHERE idempotency_key = ?",
        (invoice_key(tenant, vendor, invoice_number),),
    ).fetchone()
    return row


def invoices_by_number(
    ledger: Ledger, tenant: str, invoice_number: str, *, vendor: str | None = None
) -> list[dict]:
    """Invoice rows matching an invoice number (optionally a vendor too).

    Used by the owner status write-back, which references an invoice by the
    number the owner can see. An invoice number can repeat across vendors, so
    this returns a list; the caller disambiguates.
    """
    query = "SELECT * FROM ap_invoices WHERE tenant = ? AND invoice_number = ?"
    params: list[Any] = [tenant, invoice_number.strip()]
    if vendor:
        query += " AND vendor = ?"
        params.append(vendor)
    rows = ledger.conn.execute(query + " ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]


def insert_invoice(
    ledger: Ledger,
    *,
    tenant: str,
    vendor: str,
    invoice_number: str,
    amount_cents: int,
    invoice_date: str = "",
    due_date: str | None = None,
    gl_account: str = "",
    cost_type: str = "",
    project: str = "",
    source_file: str = "",
    source_md5: str = "",
    confidence: float | None = None,
    shadow: bool = False,
    status: str = "Received",
) -> tuple[int, bool]:
    """Insert an invoice row; a repeated identity is a no-op. -> (id, is_new)."""
    key = invoice_key(tenant, vendor, invoice_number)
    now = _now()
    cur = ledger.conn.execute(
        """
        INSERT OR IGNORE INTO ap_invoices
            (idempotency_key, tenant, vendor, invoice_number, invoice_date,
             due_date, amount_cents, gl_account, cost_type, project, status,
             source_file, source_md5, confidence, shadow, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            tenant,
            vendor,
            invoice_number,
            invoice_date,
            due_date,
            amount_cents,
            gl_account,
            cost_type,
            project,
            status,
            source_file,
            source_md5,
            confidence,
            1 if shadow else 0,
            now,
            now,
        ),
    )
    ledger.conn.commit()
    is_new = cur.rowcount == 1
    row = find_invoice(ledger, tenant, vendor, invoice_number)
    if is_new:
        history_key = f"{key}:hist:new"
        ledger.conn.execute(
            """
            INSERT OR IGNORE INTO ap_status_history
                (idempotency_key, invoice_id, status_from, status_to, actor, created_at)
            VALUES (?, ?, '(new row)', ?, 'engine', ?)
            """,
            (history_key, row["id"], status, now),
        )
        ledger.conn.commit()
    return row["id"], is_new


def update_status(
    ledger: Ledger,
    *,
    invoice_id: int,
    status_to: str,
    actor: str = "engine",
    note: str = "",
    flip_key: str | None = None,
) -> bool:
    """Flip a row's status with transition validation + one history row."""
    row = ledger.conn.execute(
        "SELECT idempotency_key, status FROM ap_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no ap_invoices row with id {invoice_id}")
    assert_transition(row["status"], status_to)
    key = flip_key or f"{row['idempotency_key']}:hist:{row['status']}->{status_to}"
    cur = ledger.conn.execute(
        """
        INSERT OR IGNORE INTO ap_status_history
            (idempotency_key, invoice_id, status_from, status_to, actor, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (key, invoice_id, row["status"], status_to, actor, note, _now()),
    )
    if cur.rowcount == 1:
        ledger.conn.execute(
            "UPDATE ap_invoices SET status = ?, updated_at = ? WHERE id = ?",
            (status_to, _now(), invoice_id),
        )
    ledger.conn.commit()
    return cur.rowcount == 1


def record_payment_details(
    ledger: Ledger,
    *,
    invoice_id: int,
    payment_date: str = "",
    check_ref: str = "",
) -> None:
    """Record how a row was (or will be) paid. Empty arguments leave the
    existing value alone, so scheduling can set a check ref today and the
    reconcile flip can add the cleared date later without erasing it."""
    row = ledger.conn.execute(
        "SELECT payment_date, check_ref FROM ap_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no ap_invoices row with id {invoice_id}")
    ledger.conn.execute(
        "UPDATE ap_invoices SET payment_date = ?, check_ref = ?, updated_at = ? WHERE id = ?",
        (
            payment_date or row["payment_date"],
            check_ref or row["check_ref"],
            _now(),
            invoice_id,
        ),
    )
    ledger.conn.commit()


def record_qbo_ids(
    ledger: Ledger,
    *,
    invoice_id: int,
    bill_id: str = "",
    payment_id: str = "",
) -> None:
    """Store ids of engine-created QBO records. Empty arguments leave the
    existing value alone. These ids are the duplicate-proofing memory (a row
    with a bill id never pushes twice) and the read side's filter (an
    engine-authored record is never clearing evidence)."""
    row = ledger.conn.execute(
        "SELECT qbo_bill_id, qbo_payment_id FROM ap_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no ap_invoices row with id {invoice_id}")
    ledger.conn.execute(
        "UPDATE ap_invoices SET qbo_bill_id = ?, qbo_payment_id = ?, updated_at = ? WHERE id = ?",
        (
            bill_id or row["qbo_bill_id"],
            payment_id or row["qbo_payment_id"],
            _now(),
            invoice_id,
        ),
    )
    ledger.conn.commit()


def engine_authored_qbo_ids(ledger: Ledger, tenant: str) -> set[str]:
    """Every QBO record id the engine itself created for this tenant."""
    rows = ledger.conn.execute(
        "SELECT qbo_bill_id, qbo_payment_id FROM ap_invoices WHERE tenant = ?", (tenant,)
    ).fetchall()
    ids = set()
    for row in rows:
        for value in (row["qbo_bill_id"], row["qbo_payment_id"]):
            if value:
                ids.add(str(value))
    return ids


def append_note_once(ledger: Ledger, *, invoice_id: int, note: str) -> bool:
    """Append ``note`` only if the row does not already carry it verbatim.

    Intake re-fires on every landing change while a flagged file sits there;
    an unconditional append grew one identical note per run (#136). Returns
    True when the note was newly appended.
    """
    row = ledger.conn.execute(
        "SELECT notes FROM ap_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is not None and note in str(row["notes"] or ""):
        return False
    append_note(ledger, invoice_id=invoice_id, note=note)
    return True


def append_note(ledger: Ledger, *, invoice_id: int, note: str) -> None:
    ledger.conn.execute(
        "UPDATE ap_invoices SET notes = CASE WHEN notes = '' THEN ? ELSE notes || ' ' || ? END,"
        " updated_at = ? WHERE id = ?",
        (note, note, _now(), invoice_id),
    )
    ledger.conn.commit()


def rows_for_verification(ledger: Ledger, tenant: str) -> list[dict]:
    """Shape ap_invoices into the verification state's ``ap_ledger`` list.

    Reads the whole tenant book, not a shadow-scoped slice: the ``shadow`` column
    is provenance (which run mode wrote the row), never a visibility gate. The
    shadow-mode safety boundary is the write guard, which blocks external writes;
    hiding rows from verification would instead make a live three-way check read
    a non-empty book as empty (the 2026-07-01 silent-empty trap).
    """
    rows = ledger.conn.execute(
        "SELECT * FROM ap_invoices WHERE tenant = ? ORDER BY id",
        (tenant,),
    ).fetchall()
    return [
        {
            "row": r["id"],
            "payee": r["vendor"],
            "invoice_ref": r["invoice_number"],
            "amount": Decimal(r["amount_cents"]) / 100,
            "status": r["status"],
            "check_ref": r["check_ref"],
        }
        for r in rows
    ]


def status_counts(ledger: Ledger, tenant: str) -> dict[str, int]:
    """Status histogram for the whole tenant book.

    Like :func:`rows_for_verification`, this does not filter ``shadow``: the tag
    is provenance, not a visibility gate (2026-07-01).
    """
    rows = ledger.conn.execute(
        "SELECT status, COUNT(*) FROM ap_invoices WHERE tenant = ? GROUP BY status",
        (tenant,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}
