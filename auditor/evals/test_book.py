"""Book-integrity lens evals: any drift between ledger and sheet is CRITICAL."""

from __future__ import annotations

from auditor.lenses import book

from .fixtures import WORKBOOK_COLUMNS, add_invoice, make_context, make_ledger


def _row(inv_id, **overrides):
    base = {
        "id": inv_id,
        "vendor": "Vendor",
        "invoice_number": "INV-1",
        "amount_cents": 662400,
        "status": "Received",
        "payment_date": "",
        "check_ref": "",
    }
    base.update(overrides)
    return base


def _check(tmp_path, sheet_rows, *, with_furniture=True):
    from .fixtures import write_workbook

    workbook = tmp_path / "book.xlsx"
    write_workbook(workbook, sheet_rows, with_furniture=with_furniture)
    ctx = make_context(
        tmp_path / "ledger",
        workbook_path=str(workbook),
        workbook_columns=WORKBOOK_COLUMNS,
    )
    with ctx.ledger:
        return book.check(ctx)


def test_faithful_sheet_is_quiet(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    a = add_invoice(conn, invoice_number="INV-1", amount_cents=662400)
    b = add_invoice(
        conn,
        invoice_number="INV-2",
        amount_cents=55234,
        status="Paid",
        payment_date="2026-07-01",
        check_ref="4042",
    )
    sheet = [
        _row(a),
        _row(
            b,
            invoice_number="INV-2",
            amount_cents=55234,
            status="Paid",
            payment_date="2026-07-01",
            check_ref="4042",
        ),
    ]
    assert _check(tmp_path, sheet) == []


def test_furniture_rows_are_not_data(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    a = add_invoice(conn, invoice_number="INV-1", amount_cents=662400)
    # subtotal + unprocessed section rows come from with_furniture=True
    assert _check(tmp_path, [_row(a)], with_furniture=True) == []


def test_amount_drift_is_critical(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    a = add_invoice(conn, invoice_number="INV-1", amount_cents=662400)
    findings = _check(tmp_path, [_row(a, amount_cents=66240)])  # off by 10x
    assert [f.condition for f in findings] == ["field-drift"]
    assert findings[0].severity == "CRITICAL"
    assert "amount" in findings[0].detail


def test_status_drift_is_critical(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    a = add_invoice(conn, invoice_number="INV-1", status="Scheduled")
    findings = _check(tmp_path, [_row(a, amount_cents=10000, status="Paid")])
    assert [f.condition for f in findings] == ["field-drift"]
    assert "status" in findings[0].detail


def test_payment_field_drift_is_critical(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    a = add_invoice(
        conn, invoice_number="INV-1", status="Paid", payment_date="2026-07-01", check_ref="4042"
    )
    findings = _check(
        tmp_path,
        [_row(a, amount_cents=10000, status="Paid", payment_date="2026-07-02", check_ref="4042")],
    )
    assert [f.condition for f in findings] == ["field-drift"]
    assert "payment date" in findings[0].detail


def test_ledger_row_missing_from_sheet_is_critical(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    add_invoice(conn, invoice_number="INV-1", amount_cents=10000)
    findings = _check(tmp_path, [])
    assert [f.condition for f in findings] == ["missing-from-sheet"]
    assert findings[0].severity == "CRITICAL"


def test_sheet_row_not_in_ledger_is_critical(tmp_path):
    make_ledger(tmp_path / "ledger")
    findings = _check(tmp_path, [_row(999, vendor="Ghost Vendor", invoice_number="GH-1")])
    assert [f.condition for f in findings] == ["not-in-ledger"]
    assert "hand" in findings[0].detail


def test_repeated_invoice_numbers_match_by_amount(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    a = add_invoice(conn, vendor="V", invoice_number="R-1", amount_cents=100)
    b = add_invoice(conn, vendor="V", invoice_number="R-1", amount_cents=200)
    sheet = [
        _row(b, vendor="V", invoice_number="R-1", amount_cents=200),
        _row(a, vendor="V", invoice_number="R-1", amount_cents=100),
    ]
    assert _check(tmp_path, sheet) == []


def test_missing_workbook_with_rows_is_critical(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    add_invoice(conn, invoice_number="INV-1")
    ctx = make_context(
        tmp_path / "ledger",
        workbook_path=str(tmp_path / "nowhere.xlsx"),
        workbook_columns=WORKBOOK_COLUMNS,
    )
    with ctx.ledger:
        findings = book.check(ctx)
    assert [f.condition for f in findings] == ["missing"]


def test_empty_book_and_no_workbook_is_quiet(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = make_context(
        tmp_path / "ledger",
        workbook_path=str(tmp_path / "nowhere.xlsx"),
        workbook_columns=WORKBOOK_COLUMNS,
    )
    with ctx.ledger:
        assert book.check(ctx) == []


def test_tenant_without_a_workbook_view_is_out_of_scope(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = make_context(tmp_path / "ledger", workbook_path="", workbook_columns=[])
    with ctx.ledger:
        assert book.check(ctx) == []


def test_dollar_round_trip():
    assert book.parse_dollars(book.dollars(662400)) == 662400
    assert book.parse_dollars("$1,234.56") == 123456
    assert book.parse_dollars("") is None
    assert book.parse_dollars("Pending total (3)") is None
