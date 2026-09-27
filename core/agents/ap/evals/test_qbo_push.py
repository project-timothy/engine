"""ap/qbo-push (write-side W1): the engine records Bills, duplicate-proof.

Contract under test (docs/qbo-write-side-design.md, merged 2026-07-17):

- The engine writes NOTHING without an approved batch card (invariant 7's
  spirit: writes to the accounting record pass the approval queue).
- Double-entry is structurally impossible: query-before-create parks
  lookalikes, provenance is stamped, ids are stored and re-runs are quiet.
- Mapping is deterministic through the vendor registry and the chart's
  fully-qualified account names; anything unmapped parks, nothing guesses.
- A readback mismatch stops the batch cold.
- The read side never treats engine-authored records as clearing evidence.

The QBO client is injected via a factory hook; no eval touches the network.
"""

from __future__ import annotations

import json

import pytest

from core.agents.ap import jobs as ap_jobs
from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"
GL = "Cost of Goods Sold:Widgets"


class FakeQbo:
    """Records writes; serves canned vendors/accounts/transactions."""

    def __init__(self):
        self.vendors = [{"id": "V9", "display_name": VENDOR}]
        self.accounts = [{"id": "A7", "fully_qualified_name": GL}]
        self.existing = []  # normalized txns for the duplicate guard
        self.created: list[dict] = []
        self.readback_amount_override = None
        self.book_close_date = ""
        self.reject_first_create = False
        self.create_returns_null_id = False
        self.readback_raises: Exception | None = None

    def fetch_vendors(self):
        return self.vendors

    def fetch_accounts(self):
        return self.accounts

    def fetch_recent_txns(self, *, since):
        return self.existing

    def fetch_book_close_date(self):
        return self.book_close_date

    def create_bill(self, payload):
        if self.reject_first_create:
            from core.adapters.qbo import QboApiError

            self.reject_first_create = False
            raise QboApiError("HTTP 400: Account Period Closed")
        bill_id = f"B{len(self.created) + 1}"
        self.created.append(payload)
        if self.create_returns_null_id:
            # #314: QBO can echo back an explicit null Id on a create.
            return {"Id": None, **payload}
        return {"Id": bill_id, **payload}

    def get_bill(self, bill_id):
        if self.readback_raises is not None:
            raise self.readback_raises
        idx = int(bill_id[1:]) - 1
        payload = dict(self.created[idx])
        if self.readback_amount_override is not None:
            payload["TotalAmt"] = self.readback_amount_override
        else:
            payload["TotalAmt"] = sum(line["Amount"] for line in payload["Line"])
        return {"Id": bill_id, **payload}


@pytest.fixture()
def fake_qbo(monkeypatch):
    fake = FakeQbo()
    monkeypatch.setattr(ap_jobs, "_qbo_write_client", lambda ctx: fake)
    return fake


def _seed(ledger_dir, *rows):
    """rows: (vendor, number, cents, status, gl, notes)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, gl, notes in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
                gl_account=gl,
                invoice_date="2026-07-10",
            )
            if notes:
                store.append_note(ledger, invoice_id=inv_id, note=notes)
    return root


def _run_push(ledger_dir, *, shadow=False):
    return run("demo", "ap", "qbo-push", shadow=shadow, ledger_dir=ledger_dir)


def _approve_pending(root):
    with Ledger.open(root) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == "ap.qbo_push_batch":
                ledger.resolve_approval("demo", card["id"], "approved")


def _row(root, number):
    with Ledger.open(root) as ledger:
        return store.invoices_by_number(ledger, "demo", number)[0]


def test_migration_adds_qbo_id_columns(tmp_path):
    root = resolve_ledger_root("demo", tmp_path / "d")
    with Ledger.open(root) as ledger:
        cols = {r[1] for r in ledger.conn.execute("PRAGMA table_info(ap_invoices)")}
    assert "qbo_bill_id" in cols and "qbo_payment_id" in cols


def test_first_run_queues_one_card_and_writes_nothing(tmp_path, fake_qbo):
    d = tmp_path / "d"
    _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))

    result = _run_push(d)

    assert result.status == "needs_approval"
    (card,) = result.approvals_needed
    assert card.action_type == "ap.qbo_push_batch"
    assert fake_qbo.created == []


def test_approved_batch_executes_with_mapping_provenance_and_ids(tmp_path, fake_qbo):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert result.status == "ok"
    (bill,) = fake_qbo.created
    assert bill["VendorRef"]["value"] == "V9"
    assert bill["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "A7"
    assert bill["Line"][0]["Amount"] == 125.00
    assert bill["DocNumber"] == "N-1"
    assert bill["PrivateNote"].startswith("engine:")
    assert _row(root, "N-1")["qbo_bill_id"] == "B1"
    with Ledger.open(root) as ledger:
        assert any(e["event_type"] == "ap.qbo.bill_created" for e in ledger.read_event_log())


def test_rerun_after_execution_pushes_nothing_new(tmp_path, fake_qbo):
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))
    _run_push(d)
    _approve_pending(root)
    _run_push(d)

    result = _run_push(d)

    assert result.status in ("ok", "noop")
    assert len(fake_qbo.created) == 1  # never a second Bill for the same row


def test_unmapped_vendor_parks_and_writes_nothing(tmp_path, fake_qbo):
    d = tmp_path / "d"
    root = _seed(d, ("Beta Freight", "B-1", 5000, "Received", GL, ""))
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert fake_qbo.created == []
    assert any(c.action_type == "ap.qbo_map_vendor" for c in result.approvals_needed)


def test_lookalike_existing_txn_parks_instead_of_writing(tmp_path, fake_qbo):
    fake_qbo.existing = [
        {"vendor": VENDOR, "amount_cents": 12500, "date": "2026-07-08", "qbo_id": "Purchase:9"}
    ]
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert fake_qbo.created == []
    assert any(c.action_type == "ap.qbo_duplicate_review" for c in result.approvals_needed)
    assert _row(root, "N-1")["qbo_bill_id"] is None


def _bill_created_events(root):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e["event_type"] == "ap.qbo.bill_created"]


def test_readback_failure_keeps_the_bill_id_and_never_repushes(tmp_path, fake_qbo):
    """Honesty audit 2026-09-03, 02-F1 (S1). The Bill exists in QBO the instant
    create returns. A readback that raises (socket timeout, DNS blip: never a
    QboApiError, so it used to escape the job and the runner recorded nothing)
    must leave the id on the row and an event naming it, so no later run can
    create the same Bill twice."""
    fake_qbo.readback_raises = TimeoutError("timed out")
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "N-1", 12500, "Received", GL, ""),
        (VENDOR, "N-2", 500, "Received", GL, ""),
    )
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert result.status == "ok"  # the job survived; nothing escaped to the runner
    assert any(a.code == "ap.qbo.readback_failed" for a in result.anomalies)
    assert _row(root, "N-1")["qbo_bill_id"] == "B1"  # the write is remembered
    assert len(fake_qbo.created) == 1  # the batch stopped at the unhealthy transport
    (ev,) = _bill_created_events(root)
    assert ev["payload"]["qbo_bill_id"] == "B1"
    assert ev["payload"]["verified"] is False
    assert "unverified 1" in result.summary

    fake_qbo.readback_raises = None
    result = _run_push(d)  # N-1 left the scope (it carries an id): a fresh run, N-2 only

    assert result.status == "ok"
    assert [b["DocNumber"] for b in fake_qbo.created] == ["N-1", "N-2"]
    assert _row(root, "N-2")["qbo_bill_id"] == "B2"


def test_readback_mismatch_keeps_the_bill_id_and_never_repushes(tmp_path, fake_qbo):
    """Retargeted 2026-09-03 (honesty audit 02-F1). The letter was "stores no
    id"; the intent, "nothing is marked verified and a human looks", survives.
    The Bill exists in QBO whatever the readback said, so the id stays on the
    row and the event says verified=False; a later scope change creates no
    second Bill for the row."""
    fake_qbo.readback_amount_override = 999.99
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert any(a.code == "ap.qbo.readback_mismatch" for a in result.anomalies)
    assert _row(root, "N-1")["qbo_bill_id"] == "B1"
    (ev,) = _bill_created_events(root)
    assert ev["payload"]["verified"] is False
    assert "pushed 0, unverified 1" in result.summary

    fake_qbo.readback_amount_override = None
    _seed(d, (VENDOR, "N-2", 700, "Received", GL, ""))  # a scope change
    _run_push(d)
    _approve_pending(root)
    _run_push(d)

    assert [b["DocNumber"] for b in fake_qbo.created] == ["N-1", "N-2"]


def test_scope_excludes_legacy_import_and_settled_rows(tmp_path, fake_qbo):
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "OPEN-1", 1000, "Received", GL, ""),
        (VENDOR, "PAID-1", 2000, "Paid", GL, ""),
        (VENDOR, "IMP-1", 3000, "Received", GL, "[legacy import 2026-07-10] x"),
    )
    result = _run_push(d)

    (card,) = result.approvals_needed
    assert "OPEN-1" in json.dumps(card.params)
    assert "PAID-1" not in json.dumps(card.params)
    assert "IMP-1" not in json.dumps(card.params)
    _approve_pending(root)
    _run_push(d)
    assert len(fake_qbo.created) == 1


def test_shadow_reports_and_writes_nothing(tmp_path, fake_qbo):
    d = tmp_path / "d"
    _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))

    result = _run_push(d, shadow=True)

    assert result.status == "ok"
    assert fake_qbo.created == []
    assert any("would push" in a for a in result.actions)


def test_bill_dated_in_a_closed_period_posts_to_the_first_open_day(tmp_path, fake_qbo):
    """2026-07-20 incident: QBO refused TxnDate 06/15 because the books are
    closed through 06/30. Standard bookkeeping: post to the first open
    period, keep the true invoice date visible on the record."""
    fake_qbo.book_close_date = "2026-06-30"
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "JUNE-1", 12500, "Received", GL, ""))
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "JUNE-1")[0]
        ledger.conn.execute(
            "UPDATE ap_invoices SET invoice_date = '2026-06-15' WHERE id = ?", (row["id"],)
        )
        ledger.conn.commit()
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert result.status == "ok"
    (bill,) = fake_qbo.created
    assert bill["TxnDate"] == "2026-07-01"  # first open day, not the closed June date
    assert "2026-06-15" in bill["PrivateNote"]  # the true invoice date stays visible
    assert _row(root, "JUNE-1")["qbo_bill_id"] == "B1"


def test_one_rejected_bill_parks_and_the_batch_continues(tmp_path, fake_qbo):
    """2026-07-20 incident: the first rejected Bill aborted the whole batch.
    A per-row rejection must park THAT row and keep writing the rest."""
    fake_qbo.reject_first_create = True
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "R-1", 1000, "Received", GL, ""),
        (VENDOR, "R-2", 2000, "Received", GL, ""),
    )
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert result.status == "ok"
    assert any(a.code == "ap.qbo.bill_rejected" for a in result.anomalies)
    assert len(fake_qbo.created) == 1  # the second row still pushed
    rows = [_row(root, "R-1"), _row(root, "R-2")]
    assert sorted(str(r["qbo_bill_id"] or "") for r in rows) == ["", "B1"]


def test_null_create_id_never_completes_the_bill(tmp_path, fake_qbo):
    """#314: a create response carrying an explicit null Id must be treated
    as absent, never as the truthy string "None"."""
    fake_qbo.create_returns_null_id = True
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "N-1", 12500, "Received", GL, ""))
    _run_push(d)
    _approve_pending(root)

    result = _run_push(d)

    assert result.status == "ok"
    assert any(a.code == "ap.qbo.create_unconfirmed" for a in result.anomalies)
    assert not _row(root, "N-1")["qbo_bill_id"]


def test_reconcile_ignores_engine_authored_records(tmp_path):
    """The engine must never read its own writing as proof money cleared."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "S-1", 12500, "Scheduled", GL, ""))
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "S-1")[0]
        store.record_qbo_ids(ledger, invoice_id=row["id"], payment_id="Purchase:777")
    evidence = tmp_path / "ev.json"
    evidence.write_text(
        json.dumps(
            [
                {
                    "qbo_id": "Purchase:777",
                    "txn_type": "Purchase",
                    "payee": VENDOR,
                    "amount_cents": 12500,
                    "date": "2026-07-15",
                    "check_ref": "",
                }
            ]
        )
    )

    result = run(
        "demo",
        "ap",
        "reconcile",
        shadow=False,
        params={"evidence_file": str(evidence)},
        ledger_dir=d,
    )

    assert _row(root, "S-1")["status"] == "Scheduled"  # not flipped by our own record
    assert result.anomalies == []
