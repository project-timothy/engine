"""Close preflight evals: checks 1, 4, 7 plus the report and the job.

Fixtures use the REAL engine ledger and the REAL auditor store (imported
here on purpose: the machinery check must read stores the auditor actually
writes — data-contract realism, while production close code touches only
the file). The accounting system is a fake with canned report JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auditor.findings import Finding
from auditor.store import AuditorStore
from core.agents.close import checks as close_checks
from core.agents.close import jobs as close_jobs
from core.agents.close.checks import CloseContext, month_bounds
from core.agents.close.report import render_preflight
from core.agents.close.schema import CheckResult, PreflightReport
from core.engine.config import CloseSettings, Identity, TenantConfig
from core.engine.contracts import JobContext
from core.engine.guard import WriteGuard
from core.ledger import Ledger
from core.ledger.event_log import append_event_line

NOW = "2026-08-02T06:00:00+00:00"


# ---- fixtures ---------------------------------------------------------------


class FakeQbo:
    def __init__(self, bills=None, reports=None, journal_entries=None, purchases=None):
        self.bills = bills or {}
        self.reports = reports or {}
        self.journal_entries = journal_entries or []
        self.purchases = purchases or []

    def fetch_bills_with_balance(self, ids):
        return {i: self.bills[i] for i in ids if i in self.bills}

    def fetch_report(self, name, params=None):
        return self.reports.get(name, {"Rows": {}})

    def fetch_journal_entries(self, *, start, end):
        return [je for je in self.journal_entries if start <= je.get("TxnDate", "") <= end]

    def fetch_purchases(self, *, start, end):
        return [p for p in self.purchases if start <= p.get("TxnDate", "") <= end]


_seq = {"n": 0}


def _key(prefix):
    _seq["n"] += 1
    return f"{prefix}:{_seq['n']}"


def _ledger(tmp_path):
    return Ledger.open(tmp_path / "ledger")


def _invoice(
    ledger,
    *,
    vendor="Vendor",
    number="INV-1",
    amount_cents=10000,
    status="Received",
    payment_date=None,
    check_ref="",
    qbo_bill_id=None,
):
    cursor = ledger.conn.execute(
        "INSERT INTO ap_invoices (idempotency_key, tenant, vendor, invoice_number, "
        "amount_cents, status, payment_date, check_ref, qbo_bill_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            _key("inv"),
            "t",
            vendor,
            number,
            amount_cents,
            status,
            payment_date,
            check_ref,
            qbo_bill_id,
            NOW,
            NOW,
        ),
    )
    ledger.conn.commit()
    return int(cursor.lastrowid)


def _paid_flip(ledger, invoice_id, *, actor="", note=""):
    ledger.conn.execute(
        "INSERT INTO ap_status_history (idempotency_key, invoice_id, status_from, status_to, "
        "actor, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (_key("hist"), invoice_id, "Scheduled", "Paid", actor, note, NOW),
    )
    ledger.conn.commit()


def _reconcile_event(ledger, invoice_id):
    append_event_line(
        ledger.root,
        {"event_type": "ap.reconcile.paid", "payload": {"invoice_id": invoice_id}},
    )


def _ctx(tmp_path, ledger, *, qbo=None, block_over_cents=50_000):
    return CloseContext(
        tenant_slug="t",
        ledger=ledger,
        month="2026-07",
        qbo=qbo or FakeQbo(),
        auditor_store=tmp_path / "aud" / "t" / "auditor.sqlite3",
        uncategorized_block_over_cents=block_over_cents,
    )


def _seed_auditor(tmp_path, findings=(), *, run_status="ok", run_at="2026-08-01T06:00:00+00:00"):
    with AuditorStore.open(tmp_path / "aud" / "t") as store:
        run_id = store.start_run("t", now=run_at)
        if run_status != "running":
            store.finish_run(run_id, status=run_status, now=run_at)
        store.reconcile("t", list(findings), now=run_at)


def _finding(severity="WARN"):
    return Finding(lens="filing", subject="thing.pdf", condition="c", severity=severity, detail="d")


# ---- check 1: machinery -----------------------------------------------------


def test_machinery_clean_is_ok(tmp_path):
    _seed_auditor(tmp_path)
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_machinery(_ctx(tmp_path, ledger), now_iso=NOW)
    assert result.status == "OK"


def test_machinery_missing_store_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_machinery(_ctx(tmp_path, ledger), now_iso=NOW)
    assert result.status == "BLOCK"
    assert "theater" in result.summary


def test_machinery_open_critical_blocks(tmp_path):
    _seed_auditor(tmp_path, [_finding("CRITICAL")])
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_machinery(_ctx(tmp_path, ledger), now_iso=NOW)
    assert result.status == "BLOCK"
    assert "thing.pdf" in " ".join(result.details)


def test_machinery_open_warn_items_warn(tmp_path):
    _seed_auditor(tmp_path, [_finding("WARN")])
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_machinery(_ctx(tmp_path, ledger), now_iso=NOW)
    assert result.status == "WARN"


def test_machinery_stale_auditor_warns(tmp_path):
    _seed_auditor(tmp_path, run_at="2026-07-25T06:00:00+00:00")  # 8 days before NOW
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_machinery(_ctx(tmp_path, ledger), now_iso=NOW)
    assert result.status == "WARN"
    assert "stale" in result.summary


# ---- check 4: AP tie-out ----------------------------------------------------


def _aging_report(total_text):
    return {
        "Rows": {
            "Row": [
                {
                    "Summary": {"ColData": [{"value": "TOTAL"}, {"value": total_text}]},
                    "type": "Section",
                }
            ]
        }
    }


def test_tie_out_clean_book_is_ok(tmp_path):
    with _ledger(tmp_path) as ledger:
        paid = _invoice(
            ledger, status="Paid", payment_date="2026-07-10", check_ref="9001", qbo_bill_id="1"
        )
        _paid_flip(ledger, paid, actor="owner")
        _invoice(ledger, number="INV-2", status="Received", amount_cents=5000, qbo_bill_id="2")
        qbo = FakeQbo(
            bills={
                "1": {"total_cents": 10000, "balance_cents": 0},
                "2": {"total_cents": 5000, "balance_cents": 5000},
            },
            reports={"AgedPayables": _aging_report("50.00")},
        )
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "OK", result.summary


def test_paid_in_month_without_evidence_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        _invoice(ledger, status="Paid", payment_date="2026-07-10")
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger))
    assert result.status == "BLOCK"
    assert "no" in result.details[0]


def test_paid_outside_month_is_out_of_scope(tmp_path):
    with _ledger(tmp_path) as ledger:
        _invoice(ledger, status="Paid", payment_date="2026-06-10")  # June, closing July
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger))
    assert result.status == "OK"


def test_reconcile_event_is_evidence(tmp_path):
    with _ledger(tmp_path) as ledger:
        paid = _invoice(ledger, status="Paid", payment_date="2026-07-10")
        _reconcile_event(ledger, paid)
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger))
    assert result.status == "OK"


def test_settled_row_with_open_bill_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        _invoice(
            ledger, status="Paid", payment_date="2026-07-10", check_ref="9001", qbo_bill_id="7"
        )
        qbo = FakeQbo(bills={"7": {"total_cents": 10000, "balance_cents": 10000}})
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "BLOCK"
    assert "still carries" in " ".join(result.details)


def test_vanished_bill_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        _invoice(ledger, status="Received", qbo_bill_id="7")
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger, qbo=FakeQbo(bills={})))
    assert result.status == "BLOCK"
    assert "no longer exists" in " ".join(result.details)


def test_aging_total_mismatch_warns(tmp_path):
    with _ledger(tmp_path) as ledger:
        _invoice(ledger, status="Received", amount_cents=5000, qbo_bill_id="2")
        qbo = FakeQbo(
            bills={"2": {"total_cents": 5000, "balance_cents": 5000}},
            reports={"AgedPayables": _aging_report("162.34")},  # extra manual bill in QBO
        )
        result = close_checks.check_ap_tie_out(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "WARN"
    assert "$162.34" in result.summary


# ---- check 7: categorization ------------------------------------------------


def _gl_report(section, amount_text, balance_text="9,876.54"):
    """Captured-shape GeneralLedger fixture.

    Column layout captured live 2026-08-24 (fetch_report("GeneralLedger",
    {start_date, end_date}) against the production book): data rows END with
    the running Balance; Amount is second-to-last. The original
    hand-transcribed fixture put the amount last — the same fixture disease
    as docs/lessons.md, "Fixtures are captured, not transcribed" — which is exactly how the
    last-numeric-cell scan in check_categorization stayed green while
    summing balances in production.
    """
    return {
        "Columns": {
            "Column": [
                {"ColTitle": "Date", "ColType": "Date"},
                {"ColTitle": "Transaction Type", "ColType": "String"},
                {"ColTitle": "Num", "ColType": "String"},
                {"ColTitle": "Name", "ColType": "String"},
                {"ColTitle": "Memo/Description", "ColType": "String"},
                {"ColTitle": "Split", "ColType": "String"},
                {"ColTitle": "Amount", "ColType": "Money"},
                {"ColTitle": "Balance", "ColType": "Money"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "Header": {"ColData": [{"value": section}]},
                    "Rows": {
                        "Row": [
                            {
                                "ColData": [
                                    {"value": "2026-07-03"},
                                    {"value": "Expense"},
                                    {"value": ""},
                                    {"value": "Vendor X"},
                                    {"value": ""},
                                    {"value": "Checking"},
                                    {"value": amount_text},
                                    {"value": balance_text},
                                ],
                                "type": "Data",
                            }
                        ]
                    },
                    "type": "Section",
                }
            ]
        },
    }


def test_categorized_month_is_ok(tmp_path):
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(reports={"GeneralLedger": _gl_report("Office Expenses", "12.00")})
        result = close_checks.check_categorization(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "OK"


def test_small_uncategorized_warns(tmp_path):
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(reports={"GeneralLedger": _gl_report("Uncategorized Expense", "123.45")})
        result = close_checks.check_categorization(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "WARN"
    assert "$123.45" in result.summary


def test_large_uncategorized_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(reports={"GeneralLedger": _gl_report("Ask My Accountant", "750.00")})
        result = close_checks.check_categorization(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "BLOCK"


def test_offender_total_is_the_amount_never_the_running_balance(tmp_path):
    """#136: QBO's GL data rows end with the running Balance column. The
    check must sum the Amount column ($123.45), not the balance the account
    happened to carry ($9,876.54) — a balance-summed WARN both misstates
    the money and can cross the BLOCK threshold on a tiny offender."""
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(
            reports={
                "GeneralLedger": _gl_report(
                    "Uncategorized Expense", "123.45", balance_text="9,876.54"
                )
            }
        )
        result = close_checks.check_categorization(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "WARN"
    assert "$123.45" in result.summary
    assert "9,876" not in result.summary


# ---- report + exit contract -------------------------------------------------


def _report(*statuses):
    return PreflightReport(
        tenant="t",
        month="2026-07",
        ran_at=NOW,
        checks=[
            CheckResult(name=f"c{i}", status=s, summary=f"s{i}") for i, s in enumerate(statuses)
        ],
    )


def test_exit_codes_follow_the_legacy_close_contract():
    assert _report("OK", "TODO").exit_code == 0
    assert _report("OK", "WARN").exit_code == 1
    assert _report("WARN", "BLOCK").exit_code == 2


def test_render_lists_unresolved_items():
    report = PreflightReport(
        tenant="t",
        month="2026-07",
        ran_at=NOW,
        checks=[
            CheckResult(name="machinery", status="OK", summary="clean"),
            CheckResult(
                name="ap-tie-out", status="BLOCK", summary="1 item", details=["row #9: no evidence"]
            ),
        ],
    )
    text = render_preflight(report)
    assert "verdict **BLOCK**" in text
    assert "- [ ] (ap-tie-out) row #9: no evidence" in text


def test_render_all_clear_says_ready():
    text = render_preflight(_report("OK", "OK"))
    assert "ready for the packet" in text


# ---- the job ----------------------------------------------------------------


def _job_ctx(tmp_path, ledger, params=None):
    tenant = TenantConfig(
        identity=Identity(legal_name="T Corp", slug="t", timezone="UTC"),
        close=CloseSettings(report_dir=str(tmp_path / "reports")),
    )
    return JobContext(
        tenant=tenant,
        tenant_slug="t",
        ledger=ledger,
        agent="close",
        job="preflight",
        params=params or {},
        guard=WriteGuard([]),
    )


def test_preflight_job_writes_report_and_records_event(tmp_path, monkeypatch):
    monkeypatch.setattr(close_jobs, "_qbo_read_client", lambda ctx: FakeQbo())
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "aud"))
    _seed_auditor(tmp_path)
    with _ledger(tmp_path) as ledger:
        output = close_jobs._preflight_run(_job_ctx(tmp_path, ledger, {"month": "2026-07"}))
    assert output.status == "ok"
    assert "close preflight 2026-07" in output.summary
    report = tmp_path / "reports" / "2026-07" / "CLOSE_PREFLIGHT.md"
    assert report.exists()
    assert output.events[0].payload["month"] == "2026-07"
    # all nine design checks render; no TODO slots remain (steps 1-3 built)
    text = report.read_text()
    assert "TODO" not in text
    for check_name in (
        "machinery",
        "statement-anchor",
        "feed-acceptance",
        "ap-tie-out",
        "payroll",
        "owner-transactions",
        "categorization",
        "expenses",
        "ar-snapshot",
    ):
        assert check_name in text


def test_preflight_job_survives_a_crashing_check(tmp_path, monkeypatch):
    class ExplodingQbo:
        def fetch_bills_with_balance(self, ids):
            raise RuntimeError("qbo down")

        def fetch_report(self, name, params=None):
            raise RuntimeError("qbo down")

    monkeypatch.setattr(close_jobs, "_qbo_read_client", lambda ctx: ExplodingQbo())
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "aud"))
    _seed_auditor(tmp_path)
    with _ledger(tmp_path) as ledger:
        _invoice(ledger, status="Received", qbo_bill_id="7")
        output = close_jobs._preflight_run(_job_ctx(tmp_path, ledger, {"month": "2026-07"}))
    assert "verdict BLOCK" in output.summary  # crash reads as BLOCK, never a dead preflight


def test_month_defaults_to_previous_calendar_month(tmp_path):
    with _ledger(tmp_path) as ledger:
        ctx = _job_ctx(tmp_path, ledger)
    assert close_jobs._closing_month(ctx, now=datetime(2026, 8, 2, 6, 0, tzinfo=UTC)) == "2026-07"
    assert close_jobs._closing_month(ctx, now=datetime(2026, 1, 15, 6, 0, tzinfo=UTC)) == "2025-12"


def test_bad_month_param_raises(tmp_path):
    with _ledger(tmp_path) as ledger:
        ctx = _job_ctx(tmp_path, ledger, {"month": "July"})
        try:
            close_jobs._closing_month(ctx)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_month_bounds():
    assert month_bounds("2026-07") == ("2026-07-01", "2026-07-31")
    assert month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
