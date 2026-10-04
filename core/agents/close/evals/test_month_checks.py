"""Evals for checks 5, 6, 8, 9: payroll, owner transactions, expenses, AR."""

from __future__ import annotations

from core.agents.close import checks as close_checks
from core.agents.close.checks import CloseContext

from .test_preflight import FakeQbo, _ledger


def _ctx(tmp_path, ledger, *, qbo=None, **overrides):
    base = dict(
        tenant_slug="t",
        ledger=ledger,
        month="2026-07",
        qbo=qbo or FakeQbo(),
        auditor_store=tmp_path / "aud" / "t" / "auditor.sqlite3",
        uncategorized_block_over_cents=50_000,
        owner_names=["Alex Owner", "Bailey Owner"],
        payroll_markers=["payrollco", "payroll"],
    )
    base.update(overrides)
    return CloseContext(**base)


def _je(date, note="Regular Payroll", doc="PayrollCo"):
    return {"TxnDate": date, "DocNumber": doc, "PrivateNote": note, "TotalAmt": 0}


def _purchase(date, payee, amount, account):
    return {
        "TxnDate": date,
        "TotalAmt": amount,
        "EntityRef": {"name": payee} if payee else None,
        "Line": [
            {"AccountBasedExpenseLineDetail": {"AccountRef": {"name": account}}} if account else {}
        ],
    }


# ---- check 5: payroll -------------------------------------------------------


def test_two_payroll_entries_ok(tmp_path):
    qbo = FakeQbo(
        journal_entries=[
            _je("2026-07-15", "Regular Payroll Jul 1 – Jul 15"),
            _je("2026-07-31", "Regular Payroll Jul 16 – Jul 31"),
        ]
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_payroll(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "OK"
    assert len(result.details) == 2


def test_one_payroll_entry_warns(tmp_path):
    qbo = FakeQbo(journal_entries=[_je("2026-07-15")])
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_payroll(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "WARN"


def test_no_payroll_entries_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_payroll(_ctx(tmp_path, ledger))
    assert result.status == "BLOCK"


def test_non_payroll_journal_entries_do_not_count(tmp_path):
    qbo = FakeQbo(
        journal_entries=[
            _je("2026-07-10", note="depreciation true-up", doc="JE-9"),
            _je("2026-07-15"),
            _je("2026-07-31"),
        ]
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_payroll(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "OK"
    assert len(result.details) == 2


# ---- check 6: owner transactions -------------------------------------------


def test_coded_owner_lines_enumerate_ok(tmp_path):
    qbo = FakeQbo(
        purchases=[
            _purchase("2026-07-02", "", "13750.00", "Loans Payable - Bailey"),
            _purchase("2026-07-02", "Alex Owner", "10000.00", "Loans Payable - Alex"),
        ]
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_owner_transactions(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "OK"
    assert len(result.details) == 2
    assert "$13,750.00" in result.details[0]


def test_uncoded_owner_line_warns(tmp_path):
    qbo = FakeQbo(purchases=[_purchase("2026-07-02", "", "5500.00", "Uncategorized Expense")])
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_owner_transactions(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "WARN"
    assert "without a real" in result.summary


def test_vendor_purchases_are_not_owner_shaped(tmp_path):
    qbo = FakeQbo(purchases=[_purchase("2026-07-02", "Some Vendor Corp", "100.00", "COGS")])
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_owner_transactions(_ctx(tmp_path, ledger, qbo=qbo))
    assert result.status == "OK"
    assert "no owner-shaped" in result.summary


# ---- check 8: expenses ------------------------------------------------------


def test_empty_or_missing_receipts_folder_ok(tmp_path):
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_expenses(
            _ctx(tmp_path, ledger, expenses_dir=str(tmp_path / "expenses"))
        )
    assert result.status == "OK"


def test_loose_receipts_warn_but_never_block(tmp_path):
    month_dir = tmp_path / "expenses" / "2026-07"
    month_dir.mkdir(parents=True)
    (month_dir / "lunch.jpg").write_bytes(b"x")
    (month_dir / "hotel.pdf").write_bytes(b"x")
    (month_dir / "notes.txt").write_bytes(b"x")  # not receipt-shaped
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_expenses(
            _ctx(tmp_path, ledger, expenses_dir=str(tmp_path / "expenses"))
        )
    assert result.status == "WARN"
    assert "2 receipt(s)" in result.summary


def test_filed_receipts_are_consumed_not_loose(tmp_path):
    """May precedent, ratified in the expenses design: receipts moved to
    _filed/<report-id>/ are consumed by a report; _cc_charges and _reference
    hold filed-not-loose paper. Only genuinely loose receipts warn."""
    month_dir = tmp_path / "expenses" / "2026-07"
    (month_dir / "_filed" / "Owner_EXP-2026-07").mkdir(parents=True)
    (month_dir / "_cc_charges").mkdir()
    (month_dir / "_reference").mkdir()
    (month_dir / "_filed" / "Owner_EXP-2026-07" / "hotel.pdf").write_bytes(b"x")
    (month_dir / "_cc_charges" / "api_vendor_receipt.pdf").write_bytes(b"x")
    (month_dir / "_reference" / "policy.pdf").write_bytes(b"x")
    (month_dir / "loose.jpg").write_bytes(b"x")
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_expenses(
            _ctx(tmp_path, ledger, expenses_dir=str(tmp_path / "expenses"))
        )
    assert result.status == "WARN"
    assert "1 receipt(s)" in result.summary
    assert "loose.jpg" in (result.details or [])


def test_all_receipts_consumed_is_ok(tmp_path):
    month_dir = tmp_path / "expenses" / "2026-07"
    (month_dir / "_filed" / "R1").mkdir(parents=True)
    (month_dir / "_filed" / "R1" / "gas.pdf").write_bytes(b"x")
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_expenses(
            _ctx(tmp_path, ledger, expenses_dir=str(tmp_path / "expenses"))
        )
    assert result.status == "OK"
    assert "consumed" in result.summary


# ---- check 9: AR snapshot ---------------------------------------------------


def _register(tmp_path, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Issued Invoices"
    ws.append(["ISSUED INVOICES"])
    ws.append(["Outgoing invoices issued"])
    ws.append([])
    ws.append(["Invoice #", "Issue Date", "Customer", "Total Amount (USD)", "Status", "File Path"])
    for row in rows:
        ws.append(row)
    path = tmp_path / "register.xlsx"
    wb.save(str(path))
    return str(path)


def test_paired_register_rows_ok_with_aging_note(tmp_path):
    pdf = tmp_path / "inv.pdf"
    pdf.write_bytes(b"pdf")
    register = _register(
        tmp_path,
        [["COG-INV-1", "2026-07-05", "Customer", 75000, "Submitted", str(pdf)]],
    )
    qbo = FakeQbo(
        reports={
            "AgedReceivables": {
                "Rows": {
                    "Row": [
                        {
                            "Summary": {"ColData": [{"value": "TOTAL"}, {"value": "75,000.00"}]},
                            "type": "Section",
                        }
                    ]
                }
            }
        }
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_ar_snapshot(
            _ctx(tmp_path, ledger, qbo=qbo, invoice_register_xlsx=register)
        )
    assert result.status == "OK"
    assert "$75,000.00" in result.summary


def test_missing_pdf_warns(tmp_path):
    register = _register(
        tmp_path,
        [["COG-INV-1", "2026-07-05", "Customer", 75000, "Submitted", "/nowhere/inv.pdf"]],
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_ar_snapshot(
            _ctx(tmp_path, ledger, invoice_register_xlsx=register)
        )
    assert result.status == "WARN"
    assert "COG-INV-1" in " ".join(result.details)


def test_placeholder_file_paths_are_skipped(tmp_path):
    register = _register(
        tmp_path,
        [["0123X", "2026-07-05", "Customer", 116200, "Pending", "(no seller-side invoice)"]],
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_ar_snapshot(
            _ctx(tmp_path, ledger, invoice_register_xlsx=register)
        )
    assert result.status == "OK"


def test_rows_outside_the_month_are_ignored(tmp_path):
    register = _register(
        tmp_path,
        [["COG-INV-0", "2026-05-07", "Customer", 100, "Paid", "/nowhere/old.pdf"]],
    )
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_ar_snapshot(
            _ctx(tmp_path, ledger, invoice_register_xlsx=register)
        )
    assert result.status == "OK"
    assert "0 issued" in result.summary


def test_unreadable_register_warns_not_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_ar_snapshot(
            _ctx(tmp_path, ledger, invoice_register_xlsx=str(tmp_path / "gone.xlsx"))
        )
    assert result.status == "WARN"
    assert "not found" in result.summary
