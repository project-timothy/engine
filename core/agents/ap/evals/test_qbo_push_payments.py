"""ap/qbo-push-payments (write side W2, phase 7 row 7.2, issue #211): the
engine records BillPayments for scheduled checks, behind a card.

Contract under test (docs/w2-billpayment-design.md; the row's acceptance):

- one check covering N bills becomes ONE BillPayment applying to all N
  (a Line per bill with a LinkedTxn to the Bill), amount = the sum, the
  check number as DocNumber, ``engine:<key>`` provenance in PrivateNote;
- a row without a ``qbo_bill_id`` is listed as skipped on the card and is
  never written (no bill to apply a payment to);
- the duplicate guard parks a card and writes nothing when a same-vendor,
  same-amount transaction already exists in QBO inside the 14-day window;
- a readback that disagrees (amount, vendor, or linked bills) stops with an
  anomaly, and the durable job record plus the stored id show the write
  happened, so the next run heals instead of writing twice;
- a second run writes nothing (the card key and the ``engine:<key>`` note
  are the idempotency memory); a death between the create call and the
  record is healed from QBO's own copy of the note;
- the job is a noop, with a clear line and no card, unless
  ``[qbo].payment_records`` is true;
- the job never flips a ledger row Paid, and reconcile never reads the
  engine's own BillPayment as clearing evidence (rule 3, extended).

The QBO client is injected via the same factory hook W1 uses; no eval
touches the network.
"""

from __future__ import annotations

import json

import pytest

from core.agents.ap import jobs as ap_jobs
from core.agents.ap import store
from core.engine.cli import main
from core.engine.config import load_tenant
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

# Today's (no authority.toml) path; #435 adds the other.
pytestmark = pytest.mark.usefixtures("demo_without_authority")

VENDOR = "Acme Tooling"
# Read from the tenant, never spelled out: the demo is rendered from the
# archetype template (row 7.19), so a hardcoded name drifts on a re-render.
BANK = load_tenant("demo").close.bank_account
CARD = "ap.qbo_payment_batch"
EVENT = "ap.qbo.payment_created"
STARTED = "ap.qbo.payment_write.started"
DONE = "ap.qbo.payment_write.done"
PAY_DATE = "2026-09-10"


class FakeQbo:
    """Records BillPayment writes; serves canned vendors, accounts, and the
    recent money-out records the duplicate guard and the note lookup read."""

    def __init__(self):
        self.vendors = [{"id": "V9", "display_name": VENDOR}]
        self.accounts = [{"id": "BANK1", "fully_qualified_name": BANK}]
        self.recent: list[dict] = []
        self.created: list[dict] = []
        self.readback_override: dict = {}
        self.readback_raises: Exception | None = None
        self.reject_create = False
        self.create_returns_null_id = False
        self.create_raises_before_write: Exception | None = None
        self.create_raises_after_write: Exception | None = None

    def fetch_vendors(self):
        return self.vendors

    def fetch_accounts(self):
        return self.accounts

    def fetch_recent_payments(self, *, since):
        return self.recent

    def create_bill_payment(self, payload):
        if self.reject_create:
            from core.adapters.qbo import QboApiError

            self.reject_create = False
            raise QboApiError("HTTP 400: Business Validation Error")
        if self.create_raises_before_write is not None:
            exc, self.create_raises_before_write = self.create_raises_before_write, None
            raise exc
        payment_id = f"BP{len(self.created) + 1}"
        self.created.append(payload)
        if self.create_raises_after_write is not None:
            exc, self.create_raises_after_write = self.create_raises_after_write, None
            raise exc
        if self.create_returns_null_id:
            # #314: QBO can echo back an explicit null Id on a create.
            return {"Id": None, **payload}
        return {"Id": payment_id, **payload}

    def get_bill_payment(self, payment_id):
        if self.readback_raises is not None:
            raise self.readback_raises
        idx = int(payment_id[2:]) - 1
        payload = dict(self.created[idx])
        payload["TotalAmt"] = sum(line["Amount"] for line in payload["Line"])
        payload.update(self.readback_override)
        return {"Id": payment_id, **payload}


@pytest.fixture()
def fake_qbo(monkeypatch):
    fake = FakeQbo()
    monkeypatch.setattr(ap_jobs, "_qbo_write_client", lambda ctx: fake)
    return fake


@pytest.fixture()
def payment_records_on(monkeypatch):
    """The demo tenant ships with the flag off (a tenant setting); flip it on
    for the run without touching tenant.toml."""
    from core.engine import runner as runner_mod

    real = runner_mod.load_tenant

    def patched(slug, *, tenants_root=None):
        cfg = real(slug, tenants_root=tenants_root)
        cfg.qbo.payment_records = True
        return cfg

    monkeypatch.setattr(runner_mod, "load_tenant", patched)


def _seed(ledger_dir, *rows, vendor=VENDOR, payment_date=PAY_DATE):
    """rows: (number, cents, status, check_ref, bill_id)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for number, cents, status, check_ref, bill_id in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
                invoice_date="2026-08-20",
            )
            if check_ref or payment_date:
                store.record_payment_details(
                    ledger, invoice_id=inv_id, payment_date=payment_date, check_ref=check_ref
                )
            if bill_id:
                store.record_qbo_ids(ledger, invoice_id=inv_id, bill_id=bill_id)
    return root


def _run(ledger_dir, *, shadow=False):
    return run("demo", "ap", "qbo-push-payments", shadow=shadow, ledger_dir=ledger_dir)


def _cards(root, status=None, action_type=CARD):
    with Ledger.open(root) as ledger:
        return [
            c
            for c in ledger.list_approvals("demo", status=status)
            if c["action_type"] == action_type
        ]


def _approve_pending(root):
    with Ledger.open(root) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == CARD:
                ledger.resolve_approval("demo", card["id"], "approved")


def _row(root, number):
    with Ledger.open(root) as ledger:
        return store.invoices_by_number(ledger, "demo", number)[0]


def _events(root, event_type=EVENT):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _records(root, record_type):
    with Ledger.open(root) as ledger:
        return ledger.job_records(tenant="demo", record_type=record_type)


def _history(root, invoice_id):
    with Ledger.open(root) as ledger:
        return ledger.conn.execute(
            "SELECT status_from, status_to FROM ap_status_history WHERE invoice_id = ? ORDER BY id",
            (invoice_id,),
        ).fetchall()


def _two_bill_check(tmp_path):
    """The 9058 shape from the live case: one check, two bills."""
    d = tmp_path / "d"
    root = _seed(
        d,
        ("N-1", 10000, "Scheduled", "9058", "B1"),
        ("N-2", 2500, "Scheduled", "9058", "B2"),
    )
    return d, root


def _executed_two_bill_check(tmp_path, fake_qbo):
    d, root = _two_bill_check(tmp_path)
    parked = _run(d)
    assert parked.status == "needs_approval"
    _approve_pending(root)
    result = _run(d)
    assert result.status == "ok", result.summary
    return d, root, result


# ---- the flag: a tenant setting, off by default -----------------------------


def test_flag_off_is_a_noop_with_a_clear_line_no_card_no_write(tmp_path, fake_qbo):
    d, root = _two_bill_check(tmp_path)

    result = _run(d)

    assert result.status == "ok"
    assert "[qbo].payment_records" in result.summary
    assert result.approvals_needed == []
    assert fake_qbo.created == []
    assert _cards(root) == []
    assert _records(root, STARTED) == []


# ---- the acceptance ----------------------------------------------------------


def test_one_check_covering_two_bills_becomes_one_billpayment(
    tmp_path, fake_qbo, payment_records_on
):
    d, root = _two_bill_check(tmp_path)

    parked = _run(d)

    assert parked.status == "needs_approval"
    (card,) = parked.approvals_needed
    assert card.action_type == CARD
    (check,) = card.params["checks"]
    assert check["check_ref"] == "9058"
    assert check["amount_cents"] == 12500
    assert sorted(check["qbo_bill_ids"]) == ["B1", "B2"]
    assert fake_qbo.created == []  # nothing writes before the owner's word

    _approve_pending(root)
    result = _run(d)

    assert result.status == "ok", result.summary
    assert result.anomalies == []
    (bp,) = fake_qbo.created
    assert bp["VendorRef"]["value"] == "V9"
    assert bp["TotalAmt"] == 125.00
    assert bp["DocNumber"] == "9058"
    assert bp["TxnDate"] == PAY_DATE
    assert bp["PayType"] == "Check"
    assert bp["CheckPayment"]["BankAccountRef"]["value"] == "BANK1"
    assert bp["PrivateNote"] == "engine:qbo-payment:9058:demo"
    linked = sorted(
        (line["LinkedTxn"][0]["TxnId"], line["LinkedTxn"][0]["TxnType"], line["Amount"])
        for line in bp["Line"]
    )
    assert linked == [("B1", "Bill", 100.00), ("B2", "Bill", 25.00)]

    n1, n2 = _row(root, "N-1"), _row(root, "N-2")
    assert n1["qbo_payment_id"] == "BillPayment:BP1"
    assert n2["qbo_payment_id"] == "BillPayment:BP1"
    # The job records; it never settles. Paid comes from clearing evidence.
    assert n1["status"] == "Scheduled" and n2["status"] == "Scheduled"
    assert len(_history(root, n1["id"])) == 1 and len(_history(root, n2["id"])) == 1

    (ev,) = _events(root)
    assert ev["payload"]["qbo_payment_id"] == "BP1"
    assert ev["payload"]["qbo_id"] == "BillPayment:BP1"
    assert ev["payload"]["check_ref"] == "9058"
    assert ev["payload"]["amount_cents"] == 12500
    assert sorted(ev["payload"]["invoice_ids"]) == sorted([n1["id"], n2["id"]])
    assert sorted(ev["payload"]["qbo_bill_ids"]) == ["B1", "B2"]
    assert ev["payload"]["payment_date"] == PAY_DATE
    assert ev["payload"]["verified"] is True
    assert ev["payload"]["engine_key"] == "qbo-payment:9058:demo"
    # Record-then-call-then-record (honesty audit 03-F9): both halves exist
    # and both name the write.
    (started,) = _records(root, STARTED)
    (done,) = _records(root, DONE)
    assert started["payload"]["engine_key"] == "qbo-payment:9058:demo"
    assert done["payload"]["qbo_payment_id"] == "BP1"
    assert started["run_id"] is not None and done["run_id"] is not None


def test_row_without_bill_id_is_skipped_on_the_card_and_never_written(
    tmp_path, fake_qbo, payment_records_on
):
    d = tmp_path / "d"
    root = _seed(
        d,
        ("N-1", 10000, "Scheduled", "9058", "B1"),
        ("VEND-1", 198917, "Scheduled", "1049", ""),  # pre-W1 history: no Bill in QBO
    )

    parked = _run(d)

    (card,) = parked.approvals_needed
    assert [c["check_ref"] for c in card.params["checks"]] == ["9058"]
    (skipped,) = card.params["skipped"]
    assert skipped["invoice_number"] == "VEND-1"
    assert skipped["check_ref"] == "1049"
    assert "no QBO bill" in skipped["reason"]

    _approve_pending(root)
    result = _run(d)

    assert result.status == "ok"
    assert [bp["DocNumber"] for bp in fake_qbo.created] == ["9058"]
    assert _row(root, "VEND-1")["qbo_payment_id"] is None
    assert "skipped 1" in result.summary


def test_duplicate_guard_parks_a_card_and_writes_nothing(tmp_path, fake_qbo, payment_records_on):
    fake_qbo.recent = [
        {
            "qbo_id": "Purchase:41",
            "vendor": VENDOR,
            "amount_cents": 12500,
            "date": "2026-09-16",  # six days after the scheduled date
            "check_ref": "",
            "linked_bill_ids": [],
            "private_note": "",
        }
    ]
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert fake_qbo.created == []
    (dup,) = [c for c in result.approvals_needed if c.action_type == "ap.qbo_duplicate_review"]
    assert dup.params["check_ref"] == "9058"
    assert dup.params["existing"] == "Purchase:41"
    assert _row(root, "N-1")["qbo_payment_id"] is None
    assert _events(root) == []


def test_duplicate_guard_window_is_fourteen_days(tmp_path, fake_qbo, payment_records_on):
    fake_qbo.recent = [
        {
            "qbo_id": "Purchase:41",
            "vendor": VENDOR,
            "amount_cents": 12500,
            "date": "2026-08-20",  # 21 days before: a different payment
            "check_ref": "",
            "linked_bill_ids": [],
            "private_note": "",
        }
    ]
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert result.status == "ok"
    assert len(fake_qbo.created) == 1
    assert not any(c.action_type == "ap.qbo_duplicate_review" for c in result.approvals_needed)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"TotalAmt": 999.99}, id="amount"),
        pytest.param({"VendorRef": {"value": "V2"}}, id="vendor"),
        pytest.param(
            {
                "Line": [
                    {
                        "Amount": 125.00,
                        "LinkedTxn": [{"TxnId": "B9", "TxnType": "Bill"}],
                    }
                ]
            },
            id="linked-bills",
        ),
    ],
)
def test_readback_mismatch_stops_with_an_anomaly_and_the_record_shows_the_write(
    tmp_path, fake_qbo, payment_records_on, override
):
    fake_qbo.readback_override = override
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert result.status == "ok"  # the job survived; nothing escaped to the runner
    assert any(a.code == "ap.qbo.payment_readback_mismatch" for a in result.anomalies)
    assert "unverified 1" in result.summary
    # The BillPayment exists whatever the readback said: the id is on every
    # covered row, the done record names it, the event says verified=False.
    assert _row(root, "N-1")["qbo_payment_id"] == "BillPayment:BP1"
    assert _row(root, "N-2")["qbo_payment_id"] == "BillPayment:BP1"
    (done,) = _records(root, DONE)
    assert done["payload"]["qbo_payment_id"] == "BP1"
    (ev,) = _events(root)
    assert ev["payload"]["verified"] is False

    fake_qbo.readback_override = {}
    again = _run(d)

    assert again.status == "ok"
    assert len(fake_qbo.created) == 1  # healed, never written twice
    assert not any(a.code.startswith("ap.qbo.payment") for a in again.anomalies)


def test_readback_failure_keeps_the_id_and_stops_the_batch(tmp_path, fake_qbo, payment_records_on):
    fake_qbo.readback_raises = TimeoutError("timed out")
    d = tmp_path / "d"
    root = _seed(
        d,
        ("N-1", 10000, "Scheduled", "9058", "B1"),
        ("N-2", 2500, "Scheduled", "9061", "B2"),
    )
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert any(a.code == "ap.qbo.payment_readback_failed" for a in result.anomalies)
    assert len(fake_qbo.created) == 1  # the batch stopped at the unhealthy transport
    assert _row(root, "N-1")["qbo_payment_id"] == "BillPayment:BP1"
    assert _row(root, "N-2")["qbo_payment_id"] is None

    fake_qbo.readback_raises = None
    result = _run(d)  # 9058 left the scope; 9061 still covered by the approved card

    assert [bp["DocNumber"] for bp in fake_qbo.created] == ["9058", "9061"]
    assert _row(root, "N-2")["qbo_payment_id"] == "BillPayment:BP2"


def test_null_create_id_stops_the_batch_not_a_fake_id(tmp_path, fake_qbo, payment_records_on):
    """#314: an explicit null Id on create must be treated as absent, never
    as the truthy string "None" persisted as the payment id."""
    fake_qbo.create_returns_null_id = True
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert any(a.code == "ap.qbo.payment_create_unconfirmed" for a in result.anomalies)
    assert _row(root, "N-1")["qbo_payment_id"] is None
    assert _row(root, "N-2")["qbo_payment_id"] is None


def test_second_run_writes_nothing(tmp_path, fake_qbo, payment_records_on):
    d, root, _ = _executed_two_bill_check(tmp_path, fake_qbo)

    again = _run(d)

    assert again.status == "ok"
    assert "nothing to record" in again.summary
    assert len(fake_qbo.created) == 1
    assert again.approvals_needed == []
    assert len(_events(root)) == 1


def test_same_scope_parks_one_card_across_runs(tmp_path, fake_qbo, payment_records_on):
    d, root = _two_bill_check(tmp_path)

    _run(d)
    _run(d)

    assert len(_cards(root, "pending")) == 1


def test_an_engine_key_already_in_qbo_heals_instead_of_writing(
    tmp_path, fake_qbo, payment_records_on
):
    """The ``engine:<key>`` note is the idempotency memory on QBO's side: a
    BillPayment carrying this check's key (a prior run whose record never
    landed) is adopted, never duplicated."""
    fake_qbo.recent = [
        {
            "qbo_id": "BillPayment:BP77",
            "vendor": VENDOR,
            "amount_cents": 12500,
            "date": PAY_DATE,
            "check_ref": "9058",
            "linked_bill_ids": ["B1", "B2"],
            "private_note": "engine:qbo-payment:9058:demo",
        }
    ]
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert result.status == "ok"
    assert fake_qbo.created == []
    assert _row(root, "N-1")["qbo_payment_id"] == "BillPayment:BP77"
    assert _row(root, "N-2")["qbo_payment_id"] == "BillPayment:BP77"
    (ev,) = _events(root)
    assert ev["payload"]["qbo_payment_id"] == "BP77"
    assert ev["payload"]["healed"] is True
    assert "healed 1" in result.summary


def test_a_death_between_the_call_and_the_record_heals_from_the_started_record(
    tmp_path, fake_qbo, payment_records_on
):
    """Record-then-call-then-record (honesty audit 2026-09-03, 03-F9 shape):
    the run dies after QBO accepted the BillPayment and before the done
    record. The started record survives in ``job_records`` linked to the
    FAILED run; the next run finds it, looks for the key in QBO, adopts
    the payment it finds, and writes nothing."""
    fake_qbo.create_raises_after_write = TimeoutError("connection reset")
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    died = _run(d)

    assert died.status == "error"
    assert len(fake_qbo.created) == 1  # QBO has it; the ledger only has the started record
    (started,) = _records(root, STARTED)
    assert started["run_id"] is not None  # linked to the FAILED run row
    assert _records(root, DONE) == []
    assert _row(root, "N-1")["qbo_payment_id"] is None

    fake_qbo.recent = [
        {
            "qbo_id": "BillPayment:BP1",
            "vendor": VENDOR,
            "amount_cents": 12500,
            "date": PAY_DATE,
            "check_ref": "9058",
            "linked_bill_ids": ["B1", "B2"],
            "private_note": "engine:qbo-payment:9058:demo",
        }
    ]
    healed = _run(d)

    assert healed.status == "ok"
    assert len(fake_qbo.created) == 1  # never a second BillPayment for the check
    assert _row(root, "N-1")["qbo_payment_id"] == "BillPayment:BP1"
    assert _row(root, "N-2")["qbo_payment_id"] == "BillPayment:BP1"
    (done,) = _records(root, DONE)
    assert done["payload"]["qbo_payment_id"] == "BP1"
    (ev,) = _events(root)
    assert ev["payload"]["healed"] is True


def test_a_death_before_the_call_landed_retries_with_the_attempt_on_record(
    tmp_path, fake_qbo, payment_records_on
):
    """The other half: the started record exists, QBO holds nothing with the
    key (the call never landed). The next run writes, and says so."""
    fake_qbo.create_raises_before_write = TimeoutError("connection refused")
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    died = _run(d)

    assert died.status == "error"
    assert fake_qbo.created == []
    assert len(_records(root, STARTED)) == 1

    retried = _run(d)

    assert retried.status == "ok"
    assert len(fake_qbo.created) == 1
    assert any(a.code == "ap.qbo.payment_write_retried" for a in retried.anomalies)
    assert _row(root, "N-1")["qbo_payment_id"] == "BillPayment:BP1"


def test_a_rejected_check_parks_and_the_batch_continues(tmp_path, fake_qbo, payment_records_on):
    fake_qbo.reject_create = True
    d = tmp_path / "d"
    root = _seed(
        d,
        ("N-1", 10000, "Scheduled", "9058", "B1"),
        ("N-2", 2500, "Scheduled", "9061", "B2"),
    )
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert result.status == "ok"
    assert any(a.code == "ap.qbo.payment_rejected" for a in result.anomalies)
    assert [bp["DocNumber"] for bp in fake_qbo.created] == ["9061"]
    assert _row(root, "N-1")["qbo_payment_id"] is None
    # The rejection closes its started record, so the next run retries the
    # check plainly instead of reporting a lost write.
    (done,) = [r for r in _records(root, DONE) if r["payload"]["engine_key"].endswith("9058:demo")]
    assert done["payload"]["qbo_payment_id"] == ""
    assert done["payload"]["rejected"]


def test_unmapped_bank_account_parks_and_writes_nothing(tmp_path, fake_qbo, payment_records_on):
    fake_qbo.accounts = [{"id": "A1", "fully_qualified_name": "Some Other Account"}]
    d, root = _two_bill_check(tmp_path)
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert fake_qbo.created == []
    (card,) = [c for c in result.approvals_needed if c.action_type == "ap.qbo_map_account"]
    assert BANK in json.dumps(card.params)
    assert _records(root, STARTED) == []


def test_unmapped_vendor_parks_and_writes_nothing(tmp_path, fake_qbo, payment_records_on):
    d = tmp_path / "d"
    root = _seed(d, ("Z-1", 5000, "Scheduled", "9070", "B5"), vendor="Zeta Freight")
    _run(d)
    _approve_pending(root)

    result = _run(d)

    assert fake_qbo.created == []
    assert any(c.action_type == "ap.qbo_map_vendor" for c in result.approvals_needed)
    assert _row(root, "Z-1")["qbo_payment_id"] is None


def test_scope_needs_a_committed_row_with_a_check_ref_and_no_payment_id(
    tmp_path, fake_qbo, payment_records_on
):
    d = tmp_path / "d"
    root = _seed(
        d,
        ("OPEN-1", 1000, "Received", "", "B1"),  # not committed: no payment exists
        ("NOREF-1", 2000, "Scheduled", "", "B2"),  # committed, but no instrument yet
        ("PAID-1", 3000, "Paid", "9050", "B3"),  # settled: money is in the books
        ("DONE-1", 4000, "Scheduled", "9051", "B4"),
        ("GO-1", 5000, "Scheduled", "9052", "B5"),
    )
    with Ledger.open(root) as ledger:
        done = store.invoices_by_number(ledger, "demo", "DONE-1")[0]
        store.record_qbo_ids(ledger, invoice_id=done["id"], payment_id="BillPayment:BP0")

    parked = _run(d)

    (card,) = parked.approvals_needed
    assert [c["check_ref"] for c in card.params["checks"]] == ["9052"]
    assert card.params["skipped"] == []


def test_shadow_reports_and_writes_nothing(tmp_path, fake_qbo, payment_records_on):
    d, root = _two_bill_check(tmp_path)

    result = _run(d, shadow=True)

    assert result.status == "ok"
    assert fake_qbo.created == []
    assert any("would record" in a for a in result.actions)
    assert _cards(root) == []
    assert _records(root, STARTED) == []


def test_approval_is_refused_when_nothing_on_the_card_is_still_writable(
    tmp_path, fake_qbo, payment_records_on, capsys
):
    """7.1's queue-side check, reused: a card whose every check has since
    been recorded (or settled) is refused at the queue, never approved and
    stuck."""
    d, root = _two_bill_check(tmp_path)
    _run(d)
    (card,) = _cards(root, "pending")
    with Ledger.open(root) as ledger:
        for number in ("N-1", "N-2"):
            row = store.invoices_by_number(ledger, "demo", number)[0]
            store.record_qbo_ids(ledger, invoice_id=row["id"], payment_id="BillPayment:BP9")

    capsys.readouterr()
    code = main(["queue", "approve", "demo", "--id", str(card["id"]), "--ledger-dir", str(d)])
    out = capsys.readouterr()

    assert code == 2
    assert "nothing on this card" in out.out + out.err
    assert _cards(root, "pending")[0]["id"] == card["id"]  # still pending


# ---- rule 3, extended: the engine's own BillPayment is never evidence ------


def test_reconcile_never_reads_the_engine_written_billpayment_as_clearing(
    tmp_path, fake_qbo, payment_records_on
):
    d, root, _ = _executed_two_bill_check(tmp_path, fake_qbo)
    evidence = tmp_path / "ev.json"
    evidence.write_text(
        json.dumps(
            [
                {
                    "qbo_id": "BillPayment:BP1",
                    "txn_type": "BillPayment",
                    "payee": VENDOR,
                    "amount_cents": 12500,
                    "date": PAY_DATE,
                    "check_ref": "9058",
                    "linked_bill_ids": ["B1", "B2"],
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

    assert result.status == "ok"
    assert result.anomalies == []
    assert result.approvals_needed == []
    assert _row(root, "N-1")["status"] == "Scheduled"
    assert _row(root, "N-2")["status"] == "Scheduled"
    assert _events(root, "ap.reconcile.paid") == []


# ---- the owner's side: a check number lands on the row at scheduling --------


def test_status_command_records_the_check_ref_and_date_with_scheduled(tmp_path):
    d = tmp_path / "d"
    root = _seed(d, ("N-1", 10000, "Received", "", ""), payment_date="")

    code = main(
        [
            "status",
            "demo",
            "N-1",
            "--scheduled",
            "--check",
            "9058",
            "--date",
            "2026-09-10",
            "--ledger-dir",
            str(d),
        ]
    )

    assert code == 0
    row = _row(root, "N-1")
    assert row["status"] == "Scheduled"
    assert row["check_ref"] == "9058"
    assert row["payment_date"] == "2026-09-10"


def test_status_command_adds_a_check_ref_to_an_already_scheduled_row(tmp_path, capsys):
    d = tmp_path / "d"
    root = _seed(d, ("N-1", 10000, "Scheduled", "", ""), payment_date="")

    capsys.readouterr()
    code = main(["status", "demo", "N-1", "--scheduled", "--check", "9058", "--ledger-dir", str(d)])
    out = capsys.readouterr()

    assert code == 0, out.err
    assert _row(root, "N-1")["check_ref"] == "9058"
    assert "check 9058" in out.out
