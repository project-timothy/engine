"""The expenses agent report + match evals (docs/expenses-design.md, approved 2026-08-04).

Contract under test:
- No card approval -> no workbook values, no ledger rows, no QBO write,
  including on re-run (invariant 7's spirit for accounting records).
- The manifest is what "consumed" means: built reports move receipts to
  `_filed/` and nothing counts twice.
- The QBO split record is duplicate-guarded, provenance-stamped, and
  readback-verified; a mismatch stops cold.
- One payment matching two open reports parks a card, never an auto-match.
- Rule 3: the engine's own Purchase never confirms its own report.
- Bank-CSV evidence flips Reimbursed; absent CSV the record holds.
- The clearing window anchors to the REPORT MONTH, never the approval day
  (payment precedes approval by design), and each CSV line clears at most
  one report (issue #133).
"""

from __future__ import annotations

import json

import pytest

from core.agents.expenses import jobs as exp_jobs
from core.engine.config import load_tenant
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

PERSON = "Pat Owner"

# The demo's own [close].bank_account: the demo tenant is rendered from the
# archetype template (row 7.19), so a spelled-out name drifts on a re-render.
BANK = load_tenant("demo").close.bank_account


class FakeQbo:
    def __init__(self):
        self.vendors = [{"id": "V1", "display_name": PERSON}]
        self.accounts = [
            {"id": "BANK", "fully_qualified_name": BANK},
            {"id": "MEALS", "fully_qualified_name": "Meals Out"},
            {"id": "TRAVEL", "fully_qualified_name": "Travel Costs"},
        ]
        self.recent: list[dict] = []
        self.evidence: list = []
        self.created: list[dict] = []
        self.readback_override = None
        self.readback_raises: Exception | None = None
        self.evidence_raises: Exception | None = None
        self.create_returns_null_id = False

    def fetch_vendors(self):
        return self.vendors

    def fetch_accounts(self):
        return self.accounts

    def fetch_recent_txns(self, *, since):
        return self.recent

    def fetch_evidence(self, *, since):
        if self.evidence_raises is not None:
            raise self.evidence_raises
        return self.evidence

    def create_purchase(self, payload):
        self.created.append(payload)
        if self.create_returns_null_id:
            # #314: QBO can echo back an explicit null Id on a create.
            return {"Id": None, **payload}
        pid = f"P{len(self.created)}"
        return {"Id": pid, **payload}

    def get_purchase(self, purchase_id):
        if self.readback_raises is not None:
            raise self.readback_raises
        idx = int(purchase_id[1:]) - 1
        payload = dict(self.created[idx])
        if self.readback_override is not None:
            payload["TotalAmt"] = self.readback_override
        else:
            payload["TotalAmt"] = sum(line["Amount"] for line in payload["Line"])
        return {"Id": purchase_id, **payload}


@pytest.fixture()
def fake_qbo(monkeypatch):
    fake = FakeQbo()
    monkeypatch.setattr(exp_jobs, "_qbo_write_client", lambda ctx: fake)
    return fake


def _params(tmp_path, **extra):
    return {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "month": "2026-08",
        "extractor": "fixture",
        **extra,
    }


def _run(job, tmp_path, **extra):
    return run(
        "demo", "expenses", job, params=_params(tmp_path, **extra), ledger_dir=tmp_path / "ledger"
    )


def _ledger(tmp_path):
    return Ledger.open(resolve_ledger_root("demo", tmp_path / "ledger"))


def _approve(tmp_path, action_type):
    with _ledger(tmp_path) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == action_type:
                ledger.resolve_approval("demo", card["id"], "approved")


def _reports(tmp_path):
    with _ledger(tmp_path) as ledger:
        rows = ledger.conn.execute("SELECT * FROM expense_report ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def _seed_receipts(tmp_path, *specs):
    """specs: (name, body, amount, category, date)"""
    person_dir = tmp_path / "drop" / PERSON / "P26_2001"
    person_dir.mkdir(parents=True, exist_ok=True)
    for name, body, *_ in specs:
        (person_dir / name).write_bytes(body)
    _run("intake", tmp_path)
    filed = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    for name, _body, amount, category, date in specs:
        (filed / (name + ".extract.json")).write_text(
            json.dumps(
                {
                    "doc_type": "receipt",
                    "confidence": 0.9,
                    "vendor_name": "Vendor",
                    "amount": amount,
                    "invoice_date": date,
                    "category": category,
                }
            )
        )
    _run("extract", tmp_path)


def _build_approved_report(tmp_path):
    _seed_receipts(
        tmp_path,
        ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"),
        ("hotel.pdf", b"h", "160.00", "Travel", "2026-08-02"),
    )
    _run("report", tmp_path, person=PERSON)
    _approve(tmp_path, "expenses.report_review")
    return _run("report", tmp_path, person=PERSON)


# ---- report ------------------------------------------------------------------


def test_report_without_approval_writes_nothing_including_rerun(tmp_path):
    _seed_receipts(tmp_path, ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"))

    first = _run("report", tmp_path, person=PERSON)
    again = _run("report", tmp_path, person=PERSON)

    assert first.status == "needs_approval"
    assert again.status == "noop"  # idempotent replay: same inputs, no second card
    assert _reports(tmp_path) == []
    assert not (tmp_path / "filing" / "2026-08" / "reports").exists()


def test_approved_report_builds_package_rows_and_consumes_receipts(tmp_path):
    result = _build_approved_report(tmp_path)

    assert result.status == "needs_approval"  # the reimbursement confirm card
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"
    assert report["total_cents"] == 20000
    report_dir = tmp_path / "filing" / "2026-08" / "reports" / "Owner_Pat_EXP-2026-08"
    assert (report_dir / "Expense_Report_2026-08.xlsx").is_file()
    assert (report_dir / "Receipts_2026-08.zip").is_file()
    manifest = json.loads((report_dir / "manifest.json").read_text())
    assert len(manifest["lines"]) == 2
    # consumed receipts moved under _filed/: nothing loose, nothing counts twice
    receipts_dir = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    assert [p for p in receipts_dir.iterdir() if p.suffix == ".pdf"] == []
    rerun = _run("report", tmp_path, person=PERSON)
    assert "nothing unconsumed" in rerun.summary


def test_survey_mode_raises_one_draft_card_per_person(tmp_path):
    _seed_receipts(tmp_path, ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"))

    result = _run("report", tmp_path)

    (card,) = result.approvals_needed
    assert card.action_type == "expenses.report_draft"
    assert card.params["person"] == PERSON


def test_possible_duplicate_scans_ride_the_review_card(tmp_path):
    _seed_receipts(
        tmp_path,
        ("scan_a.pdf", b"a", "18.50", "Meals", "2026-08-10"),
        ("scan_b.pdf", b"b", "18.50", "Meals", "2026-08-11"),
    )

    result = _run("report", tmp_path, person=PERSON)

    (card,) = result.approvals_needed
    assert "possible duplicate scans" in card.params["warnings"]


# ---- issue #110: owner-directed line splits ---------------------------------


def _sha16(body: bytes) -> str:
    import hashlib

    return hashlib.sha256(body).hexdigest()[:16]


def _approve_with_split(tmp_path, sha16, value):
    with _ledger(tmp_path) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == "expenses.report_review":
                ledger.resolve_approval(
                    "demo",
                    card["id"],
                    "approved",
                    param_overrides={f"split:{sha16}": value},
                )


def test_owner_directed_split_builds_n_lines_one_zip_entry(tmp_path):
    """#110, the live Kala shape: one $160.00 receipt split by the owner at
    approval into a $120.00 room rental and a $40.00 meal. Two receipts in,
    THREE expense_line rows out, the workbook shows the parts, the zip still
    holds one entry per receipt, and the arithmetic is code-enforced."""
    _seed_receipts(
        tmp_path,
        ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"),
        ("hotel.pdf", b"h", "160.00", "Travel", "2026-08-02"),
    )
    _run("report", tmp_path, person=PERSON)
    _approve_with_split(tmp_path, _sha16(b"h"), "120.00:Other:conference room|40.00:Meals")
    result = _run("report", tmp_path, person=PERSON)

    assert result.status == "needs_approval"  # the confirm card, as ever
    (report,) = _reports(tmp_path)
    assert report["total_cents"] == 20000  # split never changes the total
    with _ledger(tmp_path) as ledger:
        rows = ledger.conn.execute(
            "SELECT idempotency_key, amount_cents, category, note FROM expense_line "
            "WHERE report_id = ? ORDER BY id",
            (report["id"],),
        ).fetchall()
    assert len(rows) == 3
    amounts = sorted(r["amount_cents"] for r in rows)
    assert amounts == [4000, 4000, 12000]
    import re as _re

    parted = [r for r in rows if _re.search(r":line:[0-9a-f]{64}:\d+$", r["idempotency_key"])]
    assert len(parted) == 2  # the two split parts carry :<part> key suffixes
    assert {r["category"] for r in parted} == {"Other", "Meals"}
    assert any(r["note"] == "conference room" for r in parted)
    import zipfile as _zf

    report_dir = tmp_path / "filing" / "2026-08" / "reports" / "Owner_Pat_EXP-2026-08"
    with _zf.ZipFile(report_dir / "Receipts_2026-08.zip") as bundle:
        assert len(bundle.namelist()) == 2  # one entry per receipt, not per part


def test_mis_summed_split_refuses_the_build(tmp_path):
    """#110: owner values enter only through the card and code enforces the
    arithmetic (invariant 2) — parts that do not sum to the receipt refuse
    the build outright; nothing is written."""
    _seed_receipts(tmp_path, ("hotel.pdf", b"h", "160.00", "Travel", "2026-08-02"))
    _run("report", tmp_path, person=PERSON)
    _approve_with_split(tmp_path, _sha16(b"h"), "120.00:Other|10.00:Meals")
    result = _run("report", tmp_path, person=PERSON)

    assert any(a.code == "expenses.split_invalid" for a in result.anomalies)
    assert _reports(tmp_path) == []
    assert not (tmp_path / "filing" / "2026-08" / "reports").exists()


# ---- match -------------------------------------------------------------------


def test_confirmed_reimbursement_records_split_with_provenance(tmp_path, fake_qbo):
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")

    result = _run("match", tmp_path)

    assert result.status == "ok"
    (purchase,) = fake_qbo.created
    assert purchase["EntityRef"]["value"] == "V1"
    assert purchase["AccountRef"]["value"] == "BANK"
    assert purchase["PrivateNote"].startswith("engine:")
    amounts = {
        line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"]: line["Amount"]
        for line in purchase["Line"]
    }
    assert amounts == {"MEALS": 40.00, "TRAVEL": 160.00}
    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed-Recorded"
    assert report["qbo_purchase_id"] == "P1"


def test_evening_approval_books_the_local_date_never_utc_next_day(tmp_path, fake_qbo):
    """#136 UTC leak: a confirm card approved at 21:30 tenant time (demo
    tenant: America/Chicago) carries a resolved_at already on the NEXT UTC
    day. TxnDate and reimbursed_date are calendar facts on the tenant's
    wall clock — booking the UTC slice posts the reimbursement to a day on
    which nothing happened (and at month-end, to the wrong month)."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    with _ledger(tmp_path) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET resolved_at = ? WHERE action_type = ?",
            ("2026-08-13T02:30:00+00:00", "expenses.reimbursement_record"),
        )
        ledger.conn.commit()

    result = _run("match", tmp_path)

    assert result.status == "ok"
    (purchase,) = fake_qbo.created
    assert purchase["TxnDate"] == "2026-08-12"
    (report,) = _reports(tmp_path)
    assert report["reimbursed_date"] == "2026-08-12"


def test_null_create_id_never_completes_the_report(tmp_path, fake_qbo):
    """#314: dict.get(key, default) only falls back when the key is ABSENT.
    A QBO create response carrying an explicit null (`{"Id": null}`) survives
    `.get("Id", "")` as `None`, and `str(None)` is the truthy string "None" --
    which used to slip past the `if not purchase_id` guard, get remembered as
    the report's qbo_purchase_id, and then fail readback in a way that hides
    the real cause. The Id must be treated as absent, and the report must
    stay Open with the create call visible in the anomaly, not "recorded"
    under a fake id."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.create_returns_null_id = True

    result = _run("match", tmp_path)

    assert result.status == "ok"
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"
    assert report.get("qbo_purchase_id") in (None, "")
    assert any(a.code == "expenses.qbo_create_unconfirmed" for a in result.anomalies)


def test_unconfirmed_report_never_writes(tmp_path, fake_qbo):
    _build_approved_report(tmp_path)

    _run("match", tmp_path)

    assert fake_qbo.created == []
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"


def test_duplicate_guard_parks_when_owner_already_hand_coded(tmp_path, fake_qbo):
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.recent = [
        {"qbo_id": "Purchase:77", "vendor": PERSON, "amount_cents": 20000, "date": "2026-08-03"}
    ]

    result = _run("match", tmp_path)

    assert fake_qbo.created == []
    assert any(c.action_type == "expenses.qbo_duplicate_review" for c in result.approvals_needed)
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"


def _purchase_events(tmp_path, event_type):
    with _ledger(tmp_path) as ledger:
        return [e for e in ledger.read_event_log() if e["event_type"] == event_type]


def test_readback_failure_keeps_the_purchase_id_and_a_later_run_verifies(
    tmp_path, fake_qbo, monkeypatch
):
    """Honesty audit 2026-09-03, 03-F1 (S1). The Purchase exists in QBO the
    instant create returns. A readback that raises used to escape the job with
    nothing recorded, so the next run treated the engine's own Purchase as the
    owner's hand-coding and parked the report forever. Now the id is on the
    row at once (status stays Open: unverified), and the next day's run reads
    it back and completes the record. Never a second Purchase."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.readback_raises = TimeoutError("timed out")

    result = _run("match", tmp_path)

    assert result.status == "ok"
    assert any(a.code == "expenses.qbo_readback_failed" for a in result.anomalies)
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"  # never marked recorded on an unverified write
    assert report["qbo_purchase_id"] == "P1"  # but the write is remembered
    assert len(fake_qbo.created) == 1
    (ev,) = _purchase_events(tmp_path, "expense.qbo_purchase_created")
    assert ev["payload"]["verified"] is False
    assert "unverified 1" in result.summary

    retry = _run("match", tmp_path)  # the id landing is new input state: one retry
    assert retry.status == "ok"
    assert any(a.code == "expenses.qbo_readback_failed" for a in retry.anomalies)
    same_day = _run("match", tmp_path)
    assert same_day.status == "noop"  # nothing changed since: a replay, not a retry

    fake_qbo.readback_raises = None
    monkeypatch.setattr(exp_jobs, "_verify_day", lambda ctx: "2099-01-01")
    result = _run("match", tmp_path)

    assert result.status == "ok"
    assert len(fake_qbo.created) == 1  # never a second Purchase for the report
    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed-Recorded"
    assert report["qbo_purchase_id"] == "P1"
    assert report["reimbursed_channel"]  # the card's provenance landed on verify
    (ev,) = _purchase_events(tmp_path, "expense.qbo_purchase_verified")
    assert ev["payload"]["qbo_purchase_id"] == "P1"


def test_readback_mismatch_keeps_the_purchase_id_and_stays_open(tmp_path, fake_qbo, monkeypatch):
    """Retargeted 2026-09-03 (honesty audit 03-F1): the letter asserted only
    "status Open"; it now also asserts the id is retained, and that the
    next-day verify pass completes the record once QBO reads back right."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.readback_override = 999.99

    result = _run("match", tmp_path)

    assert any(a.code == "expenses.qbo_readback_mismatch" for a in result.anomalies)
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"  # never marked recorded on a bad readback
    assert report["qbo_purchase_id"] == "P1"

    fake_qbo.readback_override = None
    monkeypatch.setattr(exp_jobs, "_verify_day", lambda ctx: "2099-01-01")
    _run("match", tmp_path)

    assert len(fake_qbo.created) == 1
    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed-Recorded"


def test_rule3_unverified_own_purchase_never_confirms_a_report(tmp_path, fake_qbo):
    """An unverified Purchase is still the engine's own writing (rule 3): it
    is never clearing evidence for another report, and never a lookalike that
    parks the engine's own record as somebody else's hand-coding."""
    from core.adapters.qbo import QboEvidence

    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.readback_raises = TimeoutError("timed out")
    _run("match", tmp_path)  # P1 exists, unverified
    with _ledger(tmp_path) as ledger:
        ledger.conn.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
            "total_cents, status, created_at, updated_at) "
            "VALUES ('fresh', 'demo', ?, '2026-08', 20000, 'Open', 'now', 'now')",
            (PERSON,),
        )
        ledger.conn.commit()
    fake_qbo.evidence = [
        QboEvidence(qbo_id="P1", txn_type="Purchase", payee=PERSON, amount_cents=20000)
    ]

    result = _run("match", tmp_path)

    assert all(c.action_type != "expenses.reimbursement_record" for c in result.approvals_needed)


def test_one_payment_two_open_reports_parks_ambiguity_card(tmp_path, fake_qbo):
    from core.adapters.qbo import QboEvidence

    _build_approved_report(tmp_path)
    # a second open report at the same amount, seeded directly
    with _ledger(tmp_path) as ledger:
        ledger.conn.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
            "total_cents, status, created_at, updated_at) "
            "VALUES ('second', 'demo', ?, '2026-08', 20000, 'Open', 'now', 'now')",
            (PERSON,),
        )
        ledger.conn.commit()
    fake_qbo.evidence = [
        QboEvidence(qbo_id="Purchase:88", txn_type="Purchase", payee=PERSON, amount_cents=20000)
    ]

    result = _run("match", tmp_path)

    cards = [c.action_type for c in result.approvals_needed]
    assert "expenses.match_ambiguous" in cards
    assert "expenses.reimbursement_record" not in cards


def test_rule3_own_purchase_never_confirms_its_own_report(tmp_path, fake_qbo):
    from core.adapters.qbo import QboEvidence

    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    _run("match", tmp_path)  # writes P1, report Reimbursed-Recorded
    # a fresh open report at the same amount; the only evidence is our own P1
    with _ledger(tmp_path) as ledger:
        ledger.conn.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
            "total_cents, status, created_at, updated_at) "
            "VALUES ('fresh', 'demo', ?, '2026-08', 20000, 'Open', 'now', 'now')",
            (PERSON,),
        )
        ledger.conn.commit()
    fake_qbo.evidence = [
        QboEvidence(qbo_id="P1", txn_type="Purchase", payee=PERSON, amount_cents=20000)
    ]

    result = _run("match", tmp_path)

    assert all(c.action_type != "expenses.reimbursement_record" for c in result.approvals_needed)


def test_csv_clears_recorded_report_and_absent_csv_holds(tmp_path, fake_qbo):
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    _run("match", tmp_path)

    # absent CSV: Reimbursed-Recorded holds indefinitely
    held = _reports(tmp_path)[0]
    assert held["status"] == "Reimbursed-Recorded"

    csv = tmp_path / "bank.csv"
    csv.write_text("Posted,Memo,Chk,Value\n2026-08-20,ZELLE TO PAT,,-200.00\n")
    _run("match", tmp_path, bank_csv=str(csv))

    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed"
    assert report["cleared_date"] == "2026-08-20"


# ---- issue #115: approval corrections + config-aware match key ---------------


def _approve_with_overrides(tmp_path, action_type, overrides):
    with _ledger(tmp_path) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == action_type:
                ledger.resolve_approval("demo", card["id"], "approved", param_overrides=overrides)


def test_approval_overrides_correct_channel_and_instrument(tmp_path, fake_qbo):
    """Gap 2 live shape (2026-08-12): the card proposed the tenant default
    (Zelle) but the owner actually paid by check 3050. The correction at
    approval time must land on the report row, or the ledger records a
    channel that never happened and the check number is recorded nowhere."""
    _build_approved_report(tmp_path)
    _approve_with_overrides(
        tmp_path,
        "expenses.reimbursement_record",
        {"channel": "Check", "instrument_ref": "3050"},
    )

    result = _run("match", tmp_path)

    assert result.status == "ok"
    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed-Recorded"
    assert report["reimbursed_channel"] == "Check"
    assert report["instrument_ref"] == "3050"


def test_csv_check_number_disagreement_never_clears(tmp_path, fake_qbo):
    """A recorded report that knows its instrument (check 3050) must not be
    cleared by a same-amount CSV line carrying a DIFFERENT check number —
    that line is someone else's money. The line with the right number
    clears it."""
    _build_approved_report(tmp_path)
    _approve_with_overrides(
        tmp_path,
        "expenses.reimbursement_record",
        {"channel": "Check", "instrument_ref": "3050"},
    )
    _run("match", tmp_path)

    wrong = tmp_path / "wrong.csv"
    wrong.write_text("Posted,Memo,Chk,Value\n2026-08-20,CHECK 999,999,-200.00\n")
    _run("match", tmp_path, bank_csv=str(wrong))
    (held,) = _reports(tmp_path)
    assert held["status"] == "Reimbursed-Recorded"  # wrong check never clears

    right = tmp_path / "right.csv"
    right.write_text("Posted,Memo,Chk,Value\n2026-08-21,CHECK 3050,3050,-200.00\n")
    _run("match", tmp_path, bank_csv=str(right))
    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed"
    assert report["cleared_date"] == "2026-08-21"


def test_csv_line_without_check_number_still_clears_on_amount(tmp_path, fake_qbo):
    """Banks omit the check column on ACH/Zelle lines; a report with an
    instrument_ref still clears on an amount+date match when the line
    carries no number to disagree with (the pre-#115 behavior holds)."""
    _build_approved_report(tmp_path)
    _approve_with_overrides(
        tmp_path,
        "expenses.reimbursement_record",
        {"channel": "Check", "instrument_ref": "3050"},
    )
    _run("match", tmp_path)

    csv = tmp_path / "bank.csv"
    csv.write_text("Posted,Memo,Chk,Value\n2026-08-22,WITHDRAWAL,,-200.00\n")
    _run("match", tmp_path, bank_csv=str(csv))

    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed"


def _poke_reimbursed_date(tmp_path, report_id, date):
    with _ledger(tmp_path) as ledger:
        ledger.conn.execute(
            "UPDATE expense_report SET reimbursed_date = ? WHERE id = ?", (date, report_id)
        )
        ledger.conn.commit()


def test_2026_08_20_clearing_before_approval_day_clears(tmp_path, fake_qbo):
    """Issue #133 incident eval. The confirm card's own reason says "approve
    once the reimbursement has been paid", so payment PRECEDES approval by
    design (live shape: check 3050 cleared 8/11, card #56 approved 8/12).
    The window must anchor to the report month, never the approval day: a
    same-month line dated before reimbursed_date still clears."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    _run("match", tmp_path)
    (report,) = _reports(tmp_path)
    _poke_reimbursed_date(tmp_path, report["id"], "2026-08-12")

    csv = tmp_path / "bank.csv"
    csv.write_text("Posted,Memo,Chk,Value\n2026-08-11,ZELLE TO PAT,,-200.00\n")
    _run("match", tmp_path, bank_csv=str(csv))

    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed"
    assert report["cleared_date"] == "2026-08-11"


def test_line_before_report_month_never_clears(tmp_path, fake_qbo):
    """The month anchor keeps the stale-line protection the old bound was
    for: a same-amount line dated before the report month is someone
    else's money and must never clear the report."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    _run("match", tmp_path)

    csv = tmp_path / "bank.csv"
    csv.write_text("Posted,Memo,Chk,Value\n2026-07-15,WITHDRAWAL,,-200.00\n")
    _run("match", tmp_path, bank_csv=str(csv))

    (held,) = _reports(tmp_path)
    assert held["status"] == "Reimbursed-Recorded"


def test_one_csv_line_clears_exactly_one_report(tmp_path, fake_qbo):
    """Issue #133 sibling: a matched CSV line is consumed. One line must
    clear exactly one same-amount report, never every one of them; a
    second line clears the second report with its own date."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    _run("match", tmp_path)
    with _ledger(tmp_path) as ledger:
        ledger.conn.execute(
            "INSERT INTO expense_report (idempotency_key, tenant, person, month, "
            "total_cents, status, created_at, updated_at, qbo_purchase_id) "
            "VALUES ('twin', 'demo', ?, '2026-08', 20000, 'Reimbursed-Recorded', "
            "'now', 'now', 'P99')",
            (PERSON,),
        )
        ledger.conn.commit()

    one_line = tmp_path / "one.csv"
    one_line.write_text("Posted,Memo,Chk,Value\n2026-08-14,ZELLE TO PAT,,-200.00\n")
    _run("match", tmp_path, bank_csv=str(one_line))
    statuses = sorted(r["status"] for r in _reports(tmp_path))
    assert statuses == ["Reimbursed", "Reimbursed-Recorded"]

    two_lines = tmp_path / "two.csv"
    two_lines.write_text(
        "Posted,Memo,Chk,Value\n2026-08-14,ZELLE TO PAT,,-200.00\n"
        "2026-08-15,ZELLE TO PAT,,-200.00\n"
    )
    _run("match", tmp_path, bank_csv=str(two_lines))
    reports = _reports(tmp_path)
    assert [r["status"] for r in reports] == ["Reimbursed", "Reimbursed"]
    assert sorted(r["cleared_date"] for r in reports) == ["2026-08-14", "2026-08-15"]


def test_match_key_changes_when_category_mapping_changes(tmp_path):
    """Gap 3 (2026-08-12): a parked report could never be un-parked by a
    config fix, because the run key hashed report states but not the
    category mapping. A mapping change must produce a new key so the match
    re-runs instead of replaying the parked result."""
    from types import SimpleNamespace

    from core.engine.config import TenantConfig

    def _tenant(mapping: dict) -> TenantConfig:
        # The key declares whole config sections (#153), so the stub is a
        # real TenantConfig rather than a namespace carrying three fields.
        return TenantConfig.model_validate(
            {
                "identity": {"legal_name": "Demo", "slug": "demo"},
                "expenses": {"category_accounts": mapping},
            }
        )

    with _ledger(tmp_path) as ledger:
        base = dict(
            tenant_slug="demo",
            shadow=False,
            params={},
            ledger=ledger,
        )
        before = exp_jobs._match_key(
            SimpleNamespace(**base, tenant=_tenant({"Meals": "Meals Out"}))
        )
        after = exp_jobs._match_key(
            SimpleNamespace(
                **base,
                tenant=_tenant({"Meals": "Meals Out", "Supplies": "Project Expense - Widgets"}),
            )
        )
        same = exp_jobs._match_key(SimpleNamespace(**base, tenant=_tenant({"Meals": "Meals Out"})))
    assert before != after
    assert before == same


# ---- issue #135: the resolved month is a run input; rows precede moves -------


def test_2026_08_20_next_months_survey_is_not_a_replay_of_last_months(tmp_path, monkeypatch):
    """Issue #135: run() defaulted month to the wall clock while key()
    hashed it as '' — the identical September invocation replayed August's
    survey verbatim. The RESOLVED month rides the key."""
    _seed_receipts(tmp_path, ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"))

    monkeypatch.setattr(exp_jobs, "_tenant_month", lambda ctx: "2026-08")
    params = {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "extractor": "fixture",
    }
    august = run("demo", "expenses", "report", params=dict(params), ledger_dir=tmp_path / "ledger")
    assert august.status == "needs_approval"
    assert "2026-08" in august.summary

    monkeypatch.setattr(exp_jobs, "_tenant_month", lambda ctx: "2026-09")
    september = run(
        "demo", "expenses", "report", params=dict(params), ledger_dir=tmp_path / "ledger"
    )

    assert september.status != "noop"
    assert "2026-09" in september.summary
    assert "2026-08" not in september.summary


def test_2026_08_20_report_rows_are_committed_before_receipts_move(tmp_path, monkeypatch):
    """Issue #135: receipts moved to _filed/ before the report rows were
    inserted, so a crash in between made the retry rebuild the package
    around a silently empty receipts zip. Rows now commit first; a crash
    mid-move leaves a complete zip, the committed rows, and loose receipts
    that are visible rather than a corrupt artifact."""
    _seed_receipts(
        tmp_path,
        ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"),
        ("hotel.pdf", b"h", "160.00", "Travel", "2026-08-02"),
    )
    _run("report", tmp_path, person=PERSON)
    _approve(tmp_path, "expenses.report_review")

    real_move = exp_jobs.shutil.move
    calls = {"n": 0}

    def exploding_move(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk vanished mid-move")
        return real_move(src, dst)

    monkeypatch.setattr(exp_jobs.shutil, "move", exploding_move)
    crashed = _run("report", tmp_path, person=PERSON)
    assert crashed.status == "error"

    (report,) = _reports(tmp_path)  # the rows exist despite the crash
    assert report["total_cents"] == 20000
    import zipfile as _zipfile

    zip_path = (
        tmp_path
        / "filing"
        / "2026-08"
        / "reports"
        / "Owner_Pat_EXP-2026-08"
        / "Receipts_2026-08.zip"
    )
    with _zipfile.ZipFile(zip_path) as bundle:
        assert sorted(bundle.namelist()) == ["hotel.pdf", "meal.pdf"]  # never an empty zip

    # Honesty audit 2026-09-03, 03-F6 (S3): the crash left the report Open
    # with no confirm card and no report_built event, and every later run
    # answered "nothing unconsumed" (the rows consumed the receipts). The
    # next run must re-derive both from the Open row and finish the move.
    rerun = _run("report", tmp_path, person=PERSON)

    assert rerun.status == "needs_approval"
    with _ledger(tmp_path) as ledger:
        pending = [
            c
            for c in ledger.list_approvals("demo", status="pending")
            if c["action_type"] == "expenses.reimbursement_record"
        ]
        built = [e for e in ledger.read_event_log() if e["event_type"] == "expense.report_built"]
    assert len(pending) == 1
    assert pending[0]["params"]["report_id"] == str(report["id"])
    assert len(built) == 1
    receipts_dir = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    assert [p for p in receipts_dir.iterdir() if p.suffix == ".pdf"] == []  # move finished
    filed = list((tmp_path / "filing" / "2026-08" / "_filed").rglob("*.pdf"))
    assert sorted(p.name for p in filed) == ["hotel.pdf", "meal.pdf"]
    assert any(a.code == "expenses.report_recovered" for a in rerun.anomalies)
    steady = _run("report", tmp_path, person=PERSON)
    assert "nothing unconsumed" in steady.summary  # recovery is one-shot


# ---- honesty audit 2026-09-03: re-asks, counters, the safety net ------------


def _confirm_cards(tmp_path):
    with _ledger(tmp_path) as ledger:
        return [
            c
            for c in ledger.list_approvals("demo")
            if c["action_type"] == "expenses.reimbursement_record"
        ]


def _reject(tmp_path, card_id):
    with _ledger(tmp_path) as ledger:
        ledger.resolve_approval("demo", card_id, "rejected")


def test_rejected_confirm_card_reasks_on_a_fresh_key(tmp_path, fake_qbo):
    """03-F7 (S3), the 2026-09-01 shape on expconfirm:{report_id}: one
    reject, and every later leg-2 park collided with the rejected row and
    was swallowed while the run still listed the card. The re-ask now rides
    a fresh key naming the card it supersedes, every time."""
    from core.adapters.qbo import QboEvidence

    _build_approved_report(tmp_path)
    (first,) = _confirm_cards(tmp_path)
    _reject(tmp_path, first["id"])
    fake_qbo.evidence = [
        QboEvidence(qbo_id="Purchase:88", txn_type="Purchase", payee=PERSON, amount_cents=20000)
    ]

    result = _run("match", tmp_path)

    assert result.status == "needs_approval"
    assert [c.action_type for c in result.approvals_needed] == ["expenses.reimbursement_record"]
    assert not any(a.code == "engine.approval_swallowed" for a in result.anomalies)
    cards = _confirm_cards(tmp_path)
    assert [c["status"] for c in cards] == ["rejected", "pending"]
    assert cards[1]["idempotency_key"].endswith(f"reask-{first['id']}")

    _reject(tmp_path, cards[1]["id"])
    again = _run("match", tmp_path)

    assert again.status == "needs_approval"
    cards = _confirm_cards(tmp_path)
    assert [c["status"] for c in cards] == ["rejected", "rejected", "pending"]
    assert cards[2]["idempotency_key"].endswith(f"reask-{cards[1]['id']}")


def test_duplicate_review_card_is_record_only_and_counts_as_blocked(tmp_path, fake_qbo):
    """03-F7 (S3) + 03-F12 (S4): nothing consumes expenses.qbo_duplicate_review,
    so its reason says so, and the report it blocks is counted as blocked,
    never as a park the owner could clear by approving."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.recent = [
        {"qbo_id": "Purchase:77", "vendor": PERSON, "amount_cents": 20000, "date": "2026-08-03"}
    ]

    result = _run("match", tmp_path)

    (card,) = [
        c for c in result.approvals_needed if c.action_type == "expenses.qbo_duplicate_review"
    ]
    assert "record-only" in card.reason
    assert "Purchase:77" in card.reason
    assert "recorded 0, parked 0, blocked 1, cleared 0" in result.summary


def test_bank_unmapped_is_blocked_not_parked(tmp_path, fake_qbo):
    """03-F12 (S4). "parked N" counted anomalies that parked nothing: an
    unmapped bank account produced "parked 1" with an empty approvals list.
    The summary now reads recorded, parked, blocked, cleared, and blocked is
    the anomaly-only count."""
    _build_approved_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")
    fake_qbo.accounts = [a for a in fake_qbo.accounts if a["id"] != "BANK"]

    result = _run("match", tmp_path)

    assert result.status == "ok"
    assert result.approvals_needed == []
    assert any(a.code == "expenses.qbo_bank_unmapped" for a in result.anomalies)
    assert "recorded 0, parked 0, blocked 1, cleared 0" in result.summary
    assert fake_qbo.created == []


def test_safety_net_failure_is_an_anomaly_not_silence(tmp_path, fake_qbo):
    """03-F13 (S4). The leg-2 evidence fetch swallowed every QBO failure and
    the run read OK, so a net down for a week looked like "no evidence".
    The catch stays (recording never depends on the net) and the failure
    is now on the record."""
    _build_approved_report(tmp_path)
    fake_qbo.evidence_raises = RuntimeError("QBO 503")

    result = _run("match", tmp_path)

    assert result.status == "ok"
    (anomaly,) = [a for a in result.anomalies if a.code == "expenses.safety_net_unavailable"]
    assert "QBO 503" in anomaly.detail
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"


# ---- owner price directive (2026-09-11): a receipt the extractor could not
# price blocked the whole report with no owner path to price it -------------


def _price(tmp_path, sha16, value, **extra):
    return _run("report", tmp_path, person=PERSON, **{f"price:{sha16}": value}, **extra)


def test_unpriced_receipt_blocks_until_the_owner_prices_it(tmp_path):
    """The live 2026-09-11 shape: two receipts on one scanned page, the
    extractor found no total. Without a directive the report refuses;
    with one the owner's amount rides the review card, is recorded as an
    event, and survives to the approval run (which carries no param)."""
    _seed_receipts(
        tmp_path,
        ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"),
        ("parking_gas.pdf", b"pg", None, "Travel", "2026-08-02"),
    )
    blocked = _run("report", tmp_path, person=PERSON)
    assert "lack an amount" in blocked.summary
    assert blocked.approvals_needed == []

    result = _price(tmp_path, _sha16(b"pg"), "44.71:Travel:CVG parking $36.00 + gas $8.71")
    assert result.status == "needs_approval"
    (card,) = result.approvals_needed
    assert card.params["total_cents"] == "8471"
    assert "parking_gas.pdf $44.71 Travel" in card.params["lines"]
    assert "owner-supplied" in card.params["warnings"]
    with _ledger(tmp_path) as ledger:
        priced = [e for e in ledger.read_event_log() if e["event_type"] == "expense.owner_priced"]
    assert len(priced) == 1
    assert priced[0]["payload"]["amount_cents"] == 4471
    assert priced[0]["payload"]["previous_amount_cents"] is None

    _approve(tmp_path, "expenses.report_review")
    built = _run("report", tmp_path, person=PERSON)  # no directive: the event carries it
    assert built.status == "needs_approval"  # the confirm card
    (report,) = _reports(tmp_path)
    assert report["total_cents"] == 8471
    with _ledger(tmp_path) as ledger:
        rows = ledger.conn.execute(
            "SELECT receipt_file, amount_cents, category, note FROM expense_line "
            "WHERE report_id = ? ORDER BY id",
            (report["id"],),
        ).fetchall()
    line = next(r for r in rows if r["receipt_file"] == "parking_gas.pdf")
    assert (line["amount_cents"], line["category"]) == (4471, "Travel")


def test_owner_correction_then_split_gives_two_lines(tmp_path):
    """The food.pdf shape: the extractor read one of two receipts on the
    page ($42.78); the owner prices the scan at $75.12 and splits it at
    approval. The correction keeps the extractor's value as provenance."""
    _seed_receipts(tmp_path, ("food.pdf", b"f", "42.78", "Meals", "2026-08-03"))
    result = _price(tmp_path, _sha16(b"f"), "75.12:Meals:two receipts on one scan")
    (card,) = result.approvals_needed
    assert card.params["total_cents"] == "7512"
    with _ledger(tmp_path) as ledger:
        (priced,) = [
            e for e in ledger.read_event_log() if e["event_type"] == "expense.owner_priced"
        ]
    assert priced["payload"]["previous_amount_cents"] == 4278

    _approve_with_split(tmp_path, _sha16(b"f"), "42.78:Meals:Brank's|32.34:Meals:Captain Jacks")
    built = _run("report", tmp_path, person=PERSON)
    assert built.status == "needs_approval"
    (report,) = _reports(tmp_path)
    assert report["total_cents"] == 7512
    with _ledger(tmp_path) as ledger:
        amounts = sorted(
            r["amount_cents"]
            for r in ledger.conn.execute(
                "SELECT amount_cents FROM expense_line WHERE report_id = ?", (report["id"],)
            ).fetchall()
        )
    assert amounts == [3234, 4278]


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("abc:Travel", "not a number"),
        ("0:Travel", "must be positive"),
        ("12.00", "amount:category"),
    ],
)
def test_malformed_price_refuses_the_build(tmp_path, value, fragment):
    _seed_receipts(tmp_path, ("parking_gas.pdf", b"pg", None, "Travel", "2026-08-02"))
    result = _price(tmp_path, _sha16(b"pg"), value)
    (anomaly,) = [a for a in result.anomalies if a.code == "expenses.price_invalid"]
    assert fragment in anomaly.detail
    assert result.approvals_needed == []
    with _ledger(tmp_path) as ledger:
        assert not [e for e in ledger.read_event_log() if e["event_type"] == "expense.owner_priced"]


def test_price_naming_no_receipt_refuses_and_records_nothing(tmp_path):
    _seed_receipts(tmp_path, ("meal.pdf", b"m", "40.00", "Meals", "2026-08-01"))
    result = _price(tmp_path, "0123456789abcdef", "5.00:Meals")
    (anomaly,) = [a for a in result.anomalies if a.code == "expenses.price_invalid"]
    assert "0123456789abcdef" in anomaly.detail
    assert result.approvals_needed == []
    assert _reports(tmp_path) == []
