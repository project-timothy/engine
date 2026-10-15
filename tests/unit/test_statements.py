"""Bank statements, read by code (board t-statements, chunk 2, 2026-10-14).
Two made-up accounts in two common export shapes: the ministry's, with a
Debit and a Credit column under a bank's preamble, and the person's own,
with one signed Amount. Nothing here is a real bank's file."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.agents.expenses import statements as st

MINISTRY_CSV = """Living Water Ministry Checking ...4417
Statement period,08/01/2026 - 09/30/2026

Date,Description,Debit,Credit,Balance
08/03/2026,ONLINE GIFT HOLLIS FAMILY,,250.00,"5,250.00"
08/05/2026,REMITLY* SEND TO NEPAL,500.00,,"4,750.00"
08/12/2026,ONLINE TRANSFER TO CHK ...9921,86.40,,"4,663.60"
08/20/2026,KATHMANDU GUEST HOUSE,"1,204.50",,"3,459.10"
09/02/2026,STAPLES 0041 PRINTING,42.18,,"3,416.92"
09/02/2026,STAPLES 0041 PRINTING,42.18,,"3,374.74"
,Ending balance,,,"3,374.74"
"""

OWN_CSV = """Posted Date,Payee,Amount
2026-08-04,NETFLIX.COM,-15.49
2026-08-07,BHATBHATENI SUPERMARKET KATHMANDU,-31.62
2026-08-09,TAXI KATHMANDU,-6.80
2026-08-13,ONLINE TRANSFER FROM CHK ...4417,86.40
2026-08-15,PAYROLL DEPOSIT,"2,400.00"
2026-08-21,REMITLY,(300.00)
"""

SCHOOL_CSV = """Student,Amount,Date,School,Currency
Asha Gurung,"12,000",2026-08-18,Shree Janata School,NPR
Bikash Tamang,85.00,08/25/2026,Shree Janata School,
"""


def test_a_debit_and_credit_export_under_a_preamble_reads_every_line():
    lines = st.parse(MINISTRY_CSV)
    assert [(ln.day, ln.cents) for ln in lines[:3]] == [
        ("2026-08-03", 25000),
        ("2026-08-05", -50000),
        ("2026-08-12", -8640),
    ]
    assert lines[3].cents == -120450 and lines[3].words == "KATHMANDU GUEST HOUSE"
    assert len(lines) == 6  # the ending-balance row has no day


def test_two_lines_alike_are_two_lines_and_a_resend_is_the_same_lines():
    lines = st.parse(MINISTRY_CSV)
    twins = [ln for ln in lines if ln.day == "2026-09-02"]
    assert [ln.n for ln in twins] == [0, 1]
    shas = {ln.sha("Daniel Reyes", st.MINISTRY) for ln in lines}
    assert len(shas) == 6
    again = {ln.sha("Daniel Reyes", st.MINISTRY) for ln in st.parse(MINISTRY_CSV)}
    assert again == shas


def test_a_reworded_memo_is_still_the_same_line():
    a = st.parse("Date,Description,Amount\n2026-08-07,BHATBHATENI #12,-31.62\n")
    b = st.parse("Date,Description,Amount\n2026-08-07,BHATBHATENI SUPERMKT,-31.62\n")
    assert a[0].sha("x", st.OWN) == b[0].sha("x", st.OWN)


def test_a_signed_amount_export_reads_parentheses_and_thousands():
    lines = st.parse(OWN_CSV)
    assert [ln.cents for ln in lines] == [-1549, -3162, -680, 8640, 240000, -30000]
    assert lines[-1].kind == st.SEND
    assert lines[3].kind == st.IN and lines[0].kind == st.OUT


def test_a_file_that_is_not_a_statement_is_refused():
    with pytest.raises(st.NotAStatement):
        st.parse("hello,world\n1,2\n")
    with pytest.raises(st.NotAStatement):
        st.parse("Date,Description,Amount\n")


def test_tim_proposes_what_looks_like_the_ministrys_never_netflix():
    cats = ["Travel", "Meals", "Lodging", "Ministry Supplies"]
    looks = {ln.words: st.looks_like(ln, cats) for ln in st.parse(OWN_CSV)}
    assert looks["NETFLIX.COM"] == ""
    assert looks["TAXI KATHMANDU"] == "Travel"
    assert looks["REMITLY"] == "money sent abroad"
    assert looks["PAYROLL DEPOSIT"] == ""


def test_the_ministry_paying_the_person_back_is_matched_never_counted_twice():
    ministry, own = st.parse(MINISTRY_CSV), st.parse(OWN_CSV)
    [(paid, landed)] = st.match_transfers(ministry, own)
    assert paid.words == "ONLINE TRANSFER TO CHK ...9921" and landed.cents == 8640


def test_a_dollar_receipt_stands_behind_its_line_a_few_days_off():
    lines = {ln.sha("d", st.MINISTRY): ln for ln in st.parse(MINISTRY_CSV)}
    got = st.attach([st.Receipt("r1", "2026-08-18", 120450)], lines)
    [line] = got.values()
    assert lines[line].words == "KATHMANDU GUEST HOUSE"
    assert st.attach([st.Receipt("r1", "2026-08-10", 120450)], lines) == {}  # 10 days off
    assert st.attach([st.Receipt("r1", "2026-08-20", 120451)], lines) == {}  # a cent off


def test_a_rupee_receipt_takes_the_cards_dollars_within_the_banks_spread():
    lines = {ln.sha("d", st.OWN): ln for ln in st.parse(OWN_CSV)}
    # 4,300 rupees at the day's 141.03 is $30.49; the card charged $31.62.
    got = st.attach([st.Receipt("r", "2026-08-07", 3049, foreign=True)], lines)
    assert [lines[s].cents for s in got.values()] == [-3162]
    far = st.attach([st.Receipt("r", "2026-08-07", 2500, foreign=True)], lines)
    assert far == {}


def test_each_line_takes_one_receipt():
    lines = {ln.sha("d", st.MINISTRY): ln for ln in st.parse(MINISTRY_CSV)}
    two = [st.Receipt("a", "2026-09-02", 4218), st.Receipt("b", "2026-09-02", 4218)]
    got = st.attach(two, lines)
    assert len(got) == 2 and len(set(got.values())) == 2


def test_the_school_list_reads_each_student_with_its_currency():
    rows = st.parse_school(SCHOOL_CSV)
    assert [(r.student, r.amount, r.currency, r.day) for r in rows] == [
        ("Asha Gurung", Decimal("12000.00"), "NPR", "2026-08-18"),
        ("Bikash Tamang", Decimal("85.00"), "USD", "2026-08-25"),
    ]
    assert st.is_school_list(SCHOOL_CSV) and not st.is_school_list(OWN_CSV)
    with pytest.raises(st.NotAStatement):
        st.parse_school("Name,Amount\nx,1\n")
