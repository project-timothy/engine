"""qbo-push-payments: the engine records BillPayments (W2).

docs/w2-billpayment-design.md (phase 7 row 7.2, issue #211). When the ledger
knows a payment (a committed row wearing a check number), the engine records
the BillPayment in the accounting system so the bank-feed line arrives
pre-matched. One check paying N bills is ONE BillPayment applying to N bills.
Behind a card (invariant 7), provenance in PrivateNote (W1 rule 1),
duplicate-guarded (rule 2), read back, idempotent, and never a settle: Paid
comes from clearing evidence (rows 7.1 and 7.3), never from this job (rule 3).

Record-then-call-then-record (honesty audit 2026-09-03, 03-F9): a started
record is committed BEFORE the create call and a done record the instant it
returns, so a death anywhere leaves a trail the next run heals from (its own
records first, then the engine:<key> note the accounting system holds)
instead of writing the same check twice.

Split out of ``jobs`` (public issue #2) with its behaviour unchanged; ``jobs``
still owns the ``JOBS`` entry, the ``APPROVAL_CHECKS`` registration, and the
QuickBooks client factory, which reaches ``run`` as a callable resolved at call
time so the evals' patch of ``jobs._qbo_write_client`` lands here. The run that
used to be one function of complexity 28 is cut into steps by extraction
only: the same reads and writes in the same order, the same events and keys.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from . import store
from .inputs import retry_day, vendors
from .registry import VendorRegistry

PAYMENT_CARD = "ap.qbo_payment_batch"
PAYMENT_EVENT = "ap.qbo.payment_created"
PAYMENT_WRITE_STARTED = "ap.qbo.payment_write.started"
PAYMENT_WRITE_DONE = "ap.qbo.payment_write.done"
# W1 rule 2's window for payments: a same-vendor, same-amount money-out
# record this close to the scheduled date is the owner's own match-click or
# hand entry, so the engine parks instead of writing.
PAYMENT_DUP_WINDOW_DAYS = 14


def _payment_engine_key(tenant: str, check: str) -> str:
    """The write's idempotency key (design item 5) and, as ``engine:<key>``,
    the PrivateNote provenance. ``check`` is the normalized reference, so
    "Check 9058" and "9058" name one payment."""
    return f"qbo-payment:{check}:{tenant}"


def _payment_scope(ctx: JobContext) -> tuple[list[dict], list[dict]]:
    """(checks to record, rows skipped) from the committed rows that carry a
    check reference and no payment record yet.

    A check is recorded whole or not at all: a row without a QBO bill (pre-W1
    history) has nothing to apply a payment to, so its whole check is listed
    as skipped; a reference that names more than one vendor cannot be one
    BillPayment either. The payment date is the row's recorded date, else
    the day the owner marked it Scheduled, else the tenant's today.
    """
    from .reconcile import norm_check_ref
    from .registry import canonical_vendor as _canon
    from .status import is_committed

    rows = [
        dict(r)
        for r in ctx.ledger.conn.execute(
            """
            SELECT i.*,
                   (SELECT MAX(h.created_at) FROM ap_status_history h
                     WHERE h.invoice_id = i.id
                       AND h.status_to IN ('Scheduled', 'Scheduled in bill pay')
                       AND h.status_from != '(new row)') AS scheduled_at
              FROM ap_invoices i
             WHERE i.tenant = ? AND (i.qbo_payment_id IS NULL OR i.qbo_payment_id = '')
               AND i.check_ref != '' AND i.amount_cents > 0
             ORDER BY i.id
            """,
            (ctx.tenant_slug,),
        ).fetchall()
    ]
    rows = [r for r in rows if is_committed(str(r["status"]))]
    registry = vendors(ctx)
    by_check: dict[str, list[dict]] = {}
    for row in rows:
        by_check.setdefault(norm_check_ref(str(row["check_ref"])), []).append(row)

    def _skip(members: list[dict], reason: str) -> None:
        for r in members:
            skipped.append(
                {
                    "row_id": r["id"],
                    "vendor": str(r["vendor"]),
                    "invoice_number": str(r["invoice_number"]),
                    "check_ref": str(r["check_ref"]),
                    "amount_cents": int(r["amount_cents"]),
                    "reason": reason,
                }
            )

    groups: list[dict] = []
    skipped: list[dict] = []
    for check, members in sorted(by_check.items()):
        if not check:
            _skip(members, "unusable check reference (nothing left after normalization)")
            continue
        missing = [r for r in members if not str(r["qbo_bill_id"] or "")]
        if missing:
            named = ", ".join(f"#{r['id']} {r['invoice_number']}" for r in missing)
            _skip(
                members,
                f"no QBO bill for {named} (pre-W1 history): nothing to apply the "
                "payment to, so the check is not recorded",
            )
            continue
        if len({_canon(str(r["vendor"]), registry) for r in members}) > 1:
            _skip(
                members, "the check reference names more than one vendor; a payment has one payee"
            )
            continue
        payment_date = max((str(r["payment_date"] or "")[:10] for r in members), default="")
        if not payment_date:
            payment_date = max((str(r["scheduled_at"] or "")[:10] for r in members), default="")
        if not payment_date:
            payment_date = retry_day(ctx)
        groups.append(
            {
                "check": check,
                "check_ref": str(members[0]["check_ref"]),
                "vendor": str(members[0]["vendor"]),
                "row_ids": [int(r["id"]) for r in members],
                "invoice_numbers": [str(r["invoice_number"]) for r in members],
                "qbo_bill_ids": [str(r["qbo_bill_id"]) for r in members],
                "amount_cents": sum(int(r["amount_cents"]) for r in members),
                "payment_date": payment_date,
                "engine_key": _payment_engine_key(ctx.tenant_slug, check),
                "rows": members,
            }
        )
    return groups, skipped


def _payment_card_entry(group: dict) -> dict:
    """The structured check entry a card carries (7.1's ``payments`` shape:
    execution never re-parses display text)."""
    return {k: v for k, v in group.items() if k != "rows"}


def _payment_approved_checks(ctx: JobContext) -> dict[str, dict]:
    """Normalized check -> the approved card's entry for it (plus card id)."""
    covered: dict[str, dict] = {}
    cards = ctx.ledger.conn.execute(
        "SELECT id, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'approved' ORDER BY id",
        (ctx.tenant_slug, PAYMENT_CARD),
    ).fetchall()
    for card in cards:
        params = json.loads(card["params_json"])
        for entry in params.get("checks") or []:
            if isinstance(entry, dict) and entry.get("check"):
                covered[str(entry["check"])] = {**entry, "card_id": int(card["id"])}
    return covered


def _payment_card_covers(entry: dict | None, group: dict) -> bool:
    """An approval covers exactly the rows and amount the owner saw; a check
    whose scope moved since (a row added, an amount changed) parks afresh."""
    if entry is None:
        return False
    try:
        same_rows = sorted(int(i) for i in entry.get("row_ids", [])) == sorted(group["row_ids"])
        same_amount = int(entry.get("amount_cents", -1)) == int(group["amount_cents"])
    except (TypeError, ValueError):
        return False
    return same_rows and same_amount


def check_payment_batch(ledger, tenant: str, params: dict) -> str | None:
    """Approval-time check (``APPROVAL_CHECKS``, the 7.1 shape): a card whose
    every check has since been recorded or settled is refused at the queue,
    never approved-and-stuck. Nothing else is asked of the owner."""
    from .status import is_committed

    checks = params.get("checks")
    if not isinstance(checks, list) or not checks:
        return "this card carries no check detail; reject it and let the next run park a fresh one"
    for entry in checks:
        for raw in (entry or {}).get("row_ids", []) or []:
            row = ledger.conn.execute(
                "SELECT status, qbo_payment_id FROM ap_invoices WHERE tenant = ? AND id = ?",
                (tenant, int(raw)),
            ).fetchone()
            if row is not None and is_committed(str(row["status"])) and not row["qbo_payment_id"]:
                return None
    return (
        "nothing on this card is still writable: every row is settled or already carries "
        "a payment record; reject it"
    )


def _payment_open_writes(ctx: JobContext) -> set[str]:
    """Engine keys with a started record and no done record: a run died
    between the create call and its outcome."""
    started = {r["payload"].get("engine_key", "") for r in ctx.records(PAYMENT_WRITE_STARTED)}
    closed = {r["payload"].get("engine_key", "") for r in ctx.records(PAYMENT_WRITE_DONE)}
    return {k for k in started - closed if k}


def _payment_rejected_keys(ctx: JobContext) -> set[str]:
    """Engine keys whose latest outcome is an API rejection (no id)."""
    latest: dict[str, str] = {}
    for rec in ctx.records(PAYMENT_WRITE_DONE):
        key = str(rec["payload"].get("engine_key", ""))
        latest[key] = str(rec["payload"].get("qbo_payment_id", "") or "")
    return {k for k, pid in latest.items() if k and not pid}


def run_key(ctx: JobContext) -> str:
    """Re-runs when the scope changes OR when an approval lands (the W1
    shape). A rejected check re-runs once a day (the intake ``_retry_day``
    rule) instead of replaying its rejection forever."""
    key = RunKey(ctx, "qbopushpayments")
    key.config("qbo")
    key.config("close.bank_account")  # the account every BillPayment is drawn on
    key.config("identity.timezone")  # the payment-date fallback and the retry day
    key.param("qbo_token_file")
    key.vendors(vendors(ctx))
    if not ctx.tenant.qbo.payment_records:
        return key.digest()
    groups, skipped = _payment_scope(ctx)
    key.value(
        "scope",
        [
            f"{g['check']}:{','.join(str(i) for i in g['row_ids'])}:{g['amount_cents']}:"
            f"{','.join(g['qbo_bill_ids'])}:{g['payment_date']}"
            for g in groups
        ],
    )
    key.value("skipped", sorted(str(s["row_id"]) for s in skipped))
    approved = _payment_approved_checks(ctx)
    key.value("approved", sorted(f"{c}:{e['card_id']}" for c, e in approved.items()))
    rejected = _payment_rejected_keys(ctx)
    if any(g["engine_key"] in rejected for g in groups):
        key.value("retry_day", retry_day(ctx))
    return key.digest()


def _bill_payment_payload(group: dict, vendor_id: str, bank_account_id: str) -> dict:
    """One BillPayment by check: a Line per bill, each linked to its Bill,
    the check number as DocNumber, the engine key as provenance."""
    return {
        "VendorRef": {"value": vendor_id},
        "PayType": "Check",
        "CheckPayment": {"BankAccountRef": {"value": bank_account_id}},
        "TotalAmt": round(group["amount_cents"] / 100, 2),
        "TxnDate": group["payment_date"],
        "DocNumber": str(group["check_ref"])[:21],
        "PrivateNote": f"engine:{group['engine_key']}",
        "Line": [
            {
                "Amount": round(int(r["amount_cents"]) / 100, 2),
                "LinkedTxn": [{"TxnId": str(r["qbo_bill_id"]), "TxnType": "Bill"}],
            }
            for r in group["rows"]
        ],
    }


def _linked_bill_ids(obj: dict) -> list[str]:
    return sorted(
        {
            str(t.get("TxnId", ""))
            for line in obj.get("Line") or []
            for t in line.get("LinkedTxn") or []
            if str(t.get("TxnType", "")) == "Bill"
        }
    )


def _readback_bill_payment(
    client, payment_id: str, group: dict, vendor_id: str
) -> tuple[bool, str]:
    """Read a just-created BillPayment back and compare amount, vendor, and
    the set of linked bills with what was sent. Never raises: the record
    exists whatever the readback does, and the caller has already
    remembered its id (02-F1)."""
    try:
        readback = client.get_bill_payment(payment_id)
    except Exception as exc:  # any transport failure, not only the API's own errors
        return False, f"readback failed: {exc}"
    problems: list[str] = []
    expected = round(group["amount_cents"] / 100, 2)
    got = readback.get("TotalAmt")
    try:
        if round(float(got), 2) != expected:
            problems.append(f"amount {got!r}, expected {expected}")
    except (TypeError, ValueError):
        problems.append(f"amount {got!r}, expected {expected}")
    got_vendor = str((readback.get("VendorRef") or {}).get("value", ""))
    if got_vendor != str(vendor_id):
        problems.append(f"vendor {got_vendor!r}, expected {vendor_id!r}")
    linked = _linked_bill_ids(readback)
    if linked != sorted(group["qbo_bill_ids"]):
        problems.append(f"linked bills {linked}, expected {sorted(group['qbo_bill_ids'])}")
    if problems:
        return False, "readback " + "; ".join(problems)
    return True, "readback ok"


def _payment_label(g: dict) -> str:
    return (
        f"check {g['check_ref']} {g['vendor']} ${g['amount_cents'] / 100:,.2f} "
        f"({len(g['row_ids'])} bill(s))"
    )


def _payment_batch_card(groups: list[dict], skipped: list[dict]) -> ApprovalSpec:
    """One card for the whole outstanding scope; nothing writes until the
    owner approves it (invariant 7). Skipped rows ride the card so the
    owner sees the boundary (design: "skipped: no QBO bill")."""
    listing = "; ".join(
        f"check {g['check_ref']} {g['vendor']} ${g['amount_cents'] / 100:,.2f} "
        f"-> {', '.join(g['invoice_numbers'])}"
        for g in groups
    )
    fingerprint = "|".join(
        f"{g['check']}:{','.join(str(i) for i in sorted(g['row_ids']))}:"
        f"{g['amount_cents']}:{','.join(sorted(g['qbo_bill_ids']))}"
        for g in groups
    )
    return ApprovalSpec(
        key=f"qbopay:{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}",
        action_type=PAYMENT_CARD,
        params={
            "count": str(len(groups)),
            "checks": [_payment_card_entry(g) for g in groups],
            "listing": listing,
            "check_refs": ",".join(g["check_ref"] for g in groups),
            "skipped": skipped,
            "skipped_text": "; ".join(
                f"#{s['row_id']} {s['vendor']} {s['invoice_number']} "
                f"(check {s['check_ref']}): {s['reason']}"
                for s in skipped
            ),
        },
        reason="payment records ready to record in the accounting system",
    )


def _bank_unmapped_card(bank_name: str) -> ApprovalSpec:
    return ApprovalSpec(
        key="qbomap:a:bank",
        action_type="ap.qbo_map_account",
        params={
            "gl_account": bank_name or "(unset)",
            "role": "payment bank account ([close].bank_account)",
        },
        reason=(
            "no chart account matches the tenant's bank account; every payment "
            "record is drawn on it, so nothing writes"
        ),
    )


def _payment_lookalike(
    recent: list[dict], g: dict, registry: VendorRegistry, vendor_token: str
) -> dict | None:
    """W1 rule 2: a same-vendor, same-amount money-out record inside the
    window, the owner's own match or entry most likely."""
    from .reconcile import _to_date
    from .registry import canonical_vendor as _canon

    window = timedelta(days=PAYMENT_DUP_WINDOW_DAYS)
    pay_date = _to_date(g["payment_date"])
    for txn in recent:
        if _canon(str(txn["vendor"]), registry) != vendor_token:
            continue
        if int(txn["amount_cents"]) != int(g["amount_cents"]):
            continue
        txn_date = _to_date(txn.get("date"))
        if pay_date and txn_date and abs(txn_date - pay_date) > window:
            continue
        return txn
    return None


@dataclass
class _PaymentBatch:
    """One approved batch's live write: the inputs every check shares and
    the running outcome. ``write`` is the old loop body; each step below is
    a whole piece of it, called in the loop's own order."""

    ctx: JobContext
    client: Any
    registry: VendorRegistry
    vendor_map: dict[str, Any]
    bank_id: Any
    recent: list[dict]
    done_by_key: dict[str, dict]
    open_writes: set[str]
    actions: list[str]
    events: list[EventSpec] = field(default_factory=list)
    approvals: list[ApprovalSpec] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    recorded: int = 0
    healed: int = 0
    parked: int = 0
    unverified: int = 0

    def write(self, g: dict) -> bool:
        """Record one check. False stops the batch."""
        from .registry import canonical_vendor as _canon

        label = _payment_label(g)
        vendor_token = _canon(g["vendor"], self.registry)
        vendor_id = self.vendor_map.get(vendor_token)
        if vendor_id is None:
            self.parked += 1
            self.approvals.append(
                ApprovalSpec(
                    key=f"qbomap:v:pay:{g['check']}",
                    action_type="ap.qbo_map_vendor",
                    params={
                        "check_ref": g["check_ref"],
                        "vendor": g["vendor"],
                        "row_ids": ",".join(str(i) for i in g["row_ids"]),
                    },
                    reason="no accounting-system vendor matches this name; map or create it",
                )
            )
            return True
        healed = self._heal(g, label, vendor_token)
        if healed is None:
            return True  # a reused check number: parked, never written
        payment_id, source, verified, readback_note = healed
        if not payment_id:
            payment_id, keep_going = self._create(g, label, vendor_token, vendor_id)
            if not payment_id:
                return keep_going
        return self._finish(g, label, vendor_id, payment_id, source, verified, readback_note)

    def _heal(
        self, g: dict, label: str, vendor_token: str
    ) -> tuple[str, str, bool | None, str] | None:
        """(payment id, source, verified, readback note) for a write an
        earlier run already made; an empty id when there is none to adopt;
        None when the key names someone else's record (parked here)."""
        from .registry import canonical_vendor as _canon

        key = g["engine_key"]
        note = f"engine:{key}"
        # Heal 1: a done record names a payment the rows never received (a
        # death between the record and the row update). Same rows, same
        # amount, or it is not this write.
        prior = self.done_by_key.get(key)
        if prior is not None and _payment_card_covers(prior, g):
            return str(prior["qbo_payment_id"]), "its job record", None, ""
        # Heal 2: the accounting system already holds the engine's key
        # (a death between the create call and the record). The query
        # row is the readback; a key match on a different vendor or
        # amount is a reused check number, never adopted.
        mine = next((t for t in self.recent if str(t.get("private_note", "")) == note), None)
        if mine is None:
            return "", "", None, ""
        if _canon(str(mine["vendor"]), self.registry) == vendor_token and int(
            mine["amount_cents"]
        ) == int(g["amount_cents"]):
            payment_id = str(mine["qbo_id"]).split(":", 1)[-1]
            linked = sorted(str(b) for b in mine.get("linked_bill_ids") or [])
            verified = linked == sorted(g["qbo_bill_ids"])
            readback_note = (
                "readback ok"
                if verified
                else f"readback linked bills {linked}, expected {sorted(g['qbo_bill_ids'])}"
            )
            self.ctx.record_now(
                f"paywrite:{g['check']}:done",
                PAYMENT_WRITE_DONE,
                {
                    "engine_key": key,
                    "qbo_payment_id": payment_id,
                    "check_ref": g["check_ref"],
                    "row_ids": g["row_ids"],
                    "amount_cents": g["amount_cents"],
                    "healed": True,
                },
            )
            return payment_id, "the note", verified, readback_note
        self.parked += 1
        self.anomalies.append(
            Anomaly(
                code="ap.qbo.payment_key_collision",
                detail=(
                    f"{label}: {mine['qbo_id']} already carries {note} for a "
                    f"different vendor or amount (a reused check number); "
                    "not adopted, not written; a human looks"
                ),
            )
        )
        return None

    def _create(self, g: dict, label: str, vendor_token: str, vendor_id: Any) -> tuple[str, bool]:
        """Write the BillPayment unless a lookalike hands it to a human.
        (payment id, keep going): an empty id with True parks this check,
        with False stops the batch."""
        from ...adapters.qbo import QboApiError

        key = g["engine_key"]
        note = f"engine:{key}"
        lookalike = _payment_lookalike(self.recent, g, self.registry, vendor_token)
        if lookalike is not None:
            self.parked += 1
            self.approvals.append(
                ApprovalSpec(
                    key=f"qbopaydup:{g['check']}",
                    action_type="ap.qbo_duplicate_review",
                    params={
                        "check_ref": g["check_ref"],
                        "vendor": g["vendor"],
                        "amount": f"${g['amount_cents'] / 100:,.2f}",
                        "existing": str(lookalike["qbo_id"]),
                        "row_ids": ",".join(str(i) for i in g["row_ids"]),
                    },
                    reason=(
                        "a same-vendor, same-amount record already exists inside the "
                        f"{PAYMENT_DUP_WINDOW_DAYS}-day window (the owner's own match or "
                        "entry, most likely); a human decides, never a write"
                    ),
                )
            )
            return "", True
        if key in self.open_writes:
            self.anomalies.append(
                Anomaly(
                    code="ap.qbo.payment_write_retried",
                    detail=(
                        f"{label}: a prior run started this write and never recorded "
                        f"its outcome; the accounting system holds nothing carrying "
                        f"{note}, so the write is retried"
                    ),
                )
            )
        # The record half, durable before the call (03-F9).
        self.ctx.record_now(
            f"paywrite:{g['check']}:started",
            PAYMENT_WRITE_STARTED,
            {
                "engine_key": key,
                "check_ref": g["check_ref"],
                "vendor": g["vendor"],
                "amount_cents": g["amount_cents"],
                "row_ids": g["row_ids"],
                "qbo_bill_ids": g["qbo_bill_ids"],
                "payment_date": g["payment_date"],
            },
        )
        try:
            created = self.client.create_bill_payment(
                _bill_payment_payload(g, vendor_id, self.bank_id)
            )
        except QboApiError as exc:
            # A per-check rejection parks THAT check and the batch keeps
            # writing (the 2026-07-20 lesson). The done record closes
            # the started one so the next run retries plainly.
            self.parked += 1
            self.ctx.record_now(
                f"paywrite:{g['check']}:done",
                PAYMENT_WRITE_DONE,
                {"engine_key": key, "qbo_payment_id": "", "rejected": str(exc)},
            )
            self.anomalies.append(Anomaly(code="ap.qbo.payment_rejected", detail=f"{label}: {exc}"))
            return "", True
        # #314: same guard as the Bill/Purchase create paths.
        payment_id = str(created.get("Id") or "")
        if not payment_id:
            # A create that answered without an Id: nothing safe to
            # assume. The started record stands; the next run looks for
            # the key in the accounting system before writing (02-F1).
            self.anomalies.append(
                Anomaly(
                    code="ap.qbo.payment_create_unconfirmed",
                    detail=f"{label}: create returned no Id; batch stopped",
                )
            )
            return "", False
        # The record half after the call, BEFORE the readback and the
        # row update: the BillPayment exists the instant create returns.
        self.ctx.record_now(
            f"paywrite:{g['check']}:done",
            PAYMENT_WRITE_DONE,
            {
                "engine_key": key,
                "qbo_payment_id": payment_id,
                "check_ref": g["check_ref"],
                "row_ids": g["row_ids"],
                "amount_cents": g["amount_cents"],
            },
        )
        return payment_id, True

    def _finish(
        self,
        g: dict,
        label: str,
        vendor_id: Any,
        payment_id: str,
        source: str,
        verified: bool | None,
        readback_note: str,
    ) -> bool:
        """Put the id on every covered row, read the record back, emit the
        event. False when the readback disagrees: the batch stops."""
        key = g["engine_key"]
        for rid in g["row_ids"]:
            store.record_qbo_ids(
                self.ctx.ledger, invoice_id=rid, payment_id=f"BillPayment:{payment_id}"
            )
        if verified is None:
            verified, readback_note = _readback_bill_payment(self.client, payment_id, g, vendor_id)
        self.events.append(
            EventSpec(
                key=f"qbopay:{key}",
                event_type=PAYMENT_EVENT,
                payload={
                    "check_ref": g["check_ref"],
                    "vendor": g["vendor"],
                    "amount_cents": g["amount_cents"],
                    "qbo_payment_id": payment_id,
                    "qbo_id": f"BillPayment:{payment_id}",
                    "invoice_ids": g["row_ids"],
                    "invoice_numbers": g["invoice_numbers"],
                    "qbo_bill_ids": g["qbo_bill_ids"],
                    "payment_date": g["payment_date"],
                    "engine_key": key,
                    "verified": verified,
                    "readback": readback_note,
                    "healed": bool(source),
                },
            )
        )
        if not verified:
            code = (
                "ap.qbo.payment_readback_failed"
                if readback_note.startswith("readback failed")
                else "ap.qbo.payment_readback_mismatch"
            )
            self.anomalies.append(
                Anomaly(
                    code=code,
                    detail=f"BillPayment {payment_id} for {label}: {readback_note}; id kept on "
                    "every covered row, verify in the accounting system; batch stopped",
                )
            )
            self.unverified += 1
            self.actions.append(
                f"recorded {label} -> BillPayment {payment_id} (UNVERIFIED: {readback_note})"
            )
            return False  # the transport or the book is unhealthy; the rest waits
        if source:
            self.healed += 1
            self.actions.append(
                f"healed {label} -> BillPayment {payment_id} (adopted from {source})"
            )
        else:
            self.recorded += 1
            self.actions.append(f"recorded {label} -> BillPayment {payment_id}")
        return True


def run(ctx: JobContext, client_factory: Callable[[], Any]) -> JobOutput:
    from .reconcile import _to_date
    from .registry import canonical_vendor as _canon

    if not ctx.tenant.qbo.payment_records:
        return JobOutput(
            status="ok",
            summary="qbo-push-payments: off ([qbo].payment_records = false); no card, no write",
        )

    groups, skipped = _payment_scope(ctx)
    actions = [
        f"skipped row #{s['row_id']} {s['vendor']} / {s['invoice_number']} "
        f"(check {s['check_ref']}): {s['reason']}"
        for s in skipped
    ]
    skip_note = f", skipped {len(skipped)}" if skipped else ""

    if not groups:
        return JobOutput(
            status="ok", summary=f"qbo-push-payments: nothing to record{skip_note}", actions=actions
        )

    if ctx.shadow:
        actions.extend(f"would record {_payment_label(g)}" for g in groups)
        return JobOutput(
            status="ok",
            summary=f"qbo-push-payments: would record {len(groups)}{skip_note}",
            actions=actions,
        )

    covered = _payment_approved_checks(ctx)
    batch = [g for g in groups if _payment_card_covers(covered.get(g["check"]), g)]

    if not batch:
        return JobOutput(
            status="ok",
            summary=f"qbo-push-payments: {len(groups)} check(s) awaiting approval{skip_note}",
            approvals=[_payment_batch_card(groups, skipped)],
            actions=actions,
        )

    client = client_factory()
    registry = vendors(ctx)
    vendor_map = {_canon(v["display_name"], registry): v["id"] for v in client.fetch_vendors()}
    bank_name = str(ctx.tenant.close.bank_account or "")
    account_map = {a["fully_qualified_name"]: a["id"] for a in client.fetch_accounts()}
    bank_id = account_map.get(bank_name) if bank_name else None
    if bank_id is None:
        return JobOutput(
            status="ok",
            summary=f"qbo-push-payments: recorded 0, parked {len(batch)} "
            f"(bank account unmapped){skip_note}",
            approvals=[_bank_unmapped_card(bank_name)],
            actions=actions,
        )

    earliest = min(_to_date(g["payment_date"]) or _to_date(retry_day(ctx)) for g in batch)
    recent = client.fetch_recent_payments(
        since=(earliest - timedelta(days=PAYMENT_DUP_WINDOW_DAYS)).isoformat()
    )
    done_by_key = {
        str(r["payload"].get("engine_key", "")): r["payload"]
        for r in ctx.records(PAYMENT_WRITE_DONE)
        if r["payload"].get("qbo_payment_id")
    }
    out = _PaymentBatch(
        ctx=ctx,
        client=client,
        registry=registry,
        vendor_map=vendor_map,
        bank_id=bank_id,
        recent=recent,
        done_by_key=done_by_key,
        open_writes=_payment_open_writes(ctx),
        actions=actions,
    )
    for g in batch:
        if not out.write(g):
            break

    summary = (
        f"qbo-push-payments: recorded {out.recorded}, healed {out.healed}, parked {out.parked}"
    )
    if out.unverified:
        summary = (
            f"qbo-push-payments: recorded {out.recorded}, healed {out.healed}, "
            f"unverified {out.unverified}, parked {out.parked}"
        )
    return JobOutput(
        status="ok",
        summary=summary + skip_note,
        actions=actions,
        events=out.events,
        approvals=out.approvals,
        anomalies=out.anomalies,
    )
