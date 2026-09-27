"""Unit tests for the bank CSV adapter (config-driven format)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.adapters.bank_csv import (
    BankCsvError,
    BankCsvFormat,
    parse_bank_csv,
    to_cleared_bank_state,
)
from core.engine.config import load_tenant

FIFTY_THIRD_SHAPE = """\
Date,Description,"Check Number",Amount
04/29/2026,"ELECTRONIC IMAGE",1037,-1250.00
04/24/2026,"ACH PAYMENT VENDOR",,-300.25
04/23/2026,"DEPOSIT",,"2,500.00"
"""


def test_parse_default_format(tmp_path):
    p = tmp_path / "bank.csv"
    p.write_text(FIFTY_THIRD_SHAPE, encoding="utf-8")
    lines = parse_bank_csv(p, BankCsvFormat())
    assert [ln.date for ln in lines] == ["2026-04-29", "2026-04-24", "2026-04-23"]
    assert lines[0].check_ref == "1037"
    assert lines[1].check_ref == ""  # ACH line, no check number
    assert lines[0].amount == Decimal("-1250.00")
    assert lines[2].amount == Decimal("2500.00")  # thousands comma stripped


def test_parse_alternate_format_from_demo_tenant(tmp_path):
    fmt = load_tenant("demo").bank_csv
    p = tmp_path / "demo.csv"
    p.write_text("Posted,Memo,Chk,Value\n2026-06-01,coffee,77,-4.50\n", encoding="utf-8")
    (line,) = parse_bank_csv(p, fmt)
    assert line.date == "2026-06-01"
    assert line.check_ref == "77"
    assert line.amount == Decimal("-4.50")


def test_bad_header_and_bad_rows_raise(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("Wrong,Header\n1,2\n", encoding="utf-8")
    with pytest.raises(BankCsvError):
        parse_bank_csv(p, BankCsvFormat())
    p2 = tmp_path / "baddate.csv"
    p2.write_text('Date,Description,"Check Number",Amount\nnot-a-date,x,,1.00\n', encoding="utf-8")
    with pytest.raises(BankCsvError):
        parse_bank_csv(p2, BankCsvFormat())


def test_to_cleared_bank_state_shape(tmp_path):
    p = tmp_path / "bank.csv"
    p.write_text(FIFTY_THIRD_SHAPE, encoding="utf-8")
    state = to_cleared_bank_state(parse_bank_csv(p, BankCsvFormat()))
    assert state[0]["check_ref"] == "1037"
    assert "amount" in state[0] and "date" in state[0]


# ---- statement-file clearing evidence (phase 7 row 7.3) ---------------------


def test_line_identity_is_stable_and_keyed_on_date_amount_and_reference():
    """The identity reconcile remembers a statement line by: the same line in
    a later export is the same line; a different amount or number is not."""
    from core.adapters.bank_csv import BankLine, line_identity

    a = BankLine(date="2026-09-16", description="CHECK 9058", check_ref="9058", amount="-125.00")
    same = BankLine(date="2026-09-16", description="(memo)", check_ref="9058", amount="-125.0")
    other_amount = BankLine(date="2026-09-16", check_ref="9058", amount="-125.01")
    other_ref = BankLine(date="2026-09-16", check_ref="7059", amount="-125.00")
    assert line_identity(a).startswith("stmt:")
    assert line_identity(a) == line_identity(same)
    assert line_identity(a) != line_identity(other_amount)
    assert line_identity(a) != line_identity(other_ref)


def test_tenants_name_where_statement_exports_land():
    """The daily script resolves the statement folder from tenant config,
    never from a literal; the demo carries a fixture-safe relative value."""
    demo = load_tenant("demo").bank_csv
    assert demo.statement_dir and not demo.statement_dir.startswith("/")
