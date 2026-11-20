"""A bank statement's photo or PDF, read into lines that add up (Standalone
Tim, card R2).

A model's reading of a statement is a list of dollar amounts, and no model
output becomes a number in the books (engine invariant 2). So these tests
attack the code check first: the opening balance plus the lines must be the
closing balance the statement prints, to the cent, every line inside the
statement's own period, or nothing is offered. Then the page: a statement
that adds up is offered for one tap, and her tap holds exactly the lines the
bank's CSV would have made; one that doesn't is turned away with plain words
asking for the CSV, and nothing of it is kept. With the reader off, failed,
or capped, the catch-up link behaves exactly as it did.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from core.agents.expenses import statements as st
from core.ledger.ledger import DB_FILENAME
from core.tools import statement_read as sr
from core.tools.receipts import page, upload
from tests.unit.test_bank_catch_up import _daniel, _events, _held
from tests.unit.test_catch_up import _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_statements import MINISTRY_CSV

TODAY = date(2026, 10, 14)
PDF = b"%PDF-1.4\n% a statement, as a phone sends it\n" + bytes(range(256)) * 8

# MINISTRY_CSV, as a model reads the same statement off a picture of it: the
# CSV fixture's own lines (no merchant's name retyped here), money as text.
LINES = [(ln.day, ln.words, f"{ln.cents / 100:.2f}") for ln in st.parse(MINISTRY_CSV)]


def _reading(**over) -> dict:
    reply = {
        "doc_type": "bank_statement",
        "bank": "Lantern Prairie Bank",
        "currency": "USD",
        "period_start": "2026-08-01",
        "period_end": "2026-09-30",
        "opening_balance": "5000.00",
        "closing_balance": "3374.74",
        "total_in": "250.00",
        "total_out": "1875.26",
        "lines": [{"day": d, "words": w, "amount": a} for d, w, a in LINES],
    }
    return {**reply, **over}


def _check(**over):
    return sr.check(sr.StatementReply(**_reading(**over)), TODAY)


def _lines(*changes: tuple[int, str, str]) -> list[dict]:
    lines = [{"day": d, "words": w, "amount": a} for d, w, a in LINES]
    for at, key, value in changes:
        lines[at] = {**lines[at], key: value}
    return lines


# ---- the code check: the whole money rule ------------------------------------------------


def test_a_reading_that_adds_up_is_exactly_the_lines_the_csv_makes():
    proven, why = _check()
    assert why == "" and proven is not None
    assert proven.rows == tuple(st.parse(MINISTRY_CSV))
    assert [(r.day, r.words, r.cents, r.n) for r in proven.rows] == [
        (r.day, r.words, r.cents, r.n) for r in st.parse(MINISTRY_CSV)
    ]
    # the two Staples lines on one day for one amount are told apart, as the CSV does
    assert [r.n for r in proven.rows][-2:] == [0, 1]
    # the closing balance rides the last day's line, as a Balance column gives it
    assert proven.rows[-1].day_end == 337474 == st.parse(MINISTRY_CSV)[-1].day_end
    assert (proven.opening, proven.closing) == (500000, 337474)
    assert (proven.start, proven.end) == (date(2026, 8, 1), date(2026, 9, 30))


@pytest.mark.parametrize(
    ("over", "why"),
    [
        # one cent off at either end
        ({"closing_balance": "3374.73"}, "the lines don't add up to the balances"),
        ({"opening_balance": "5000.01"}, "the lines don't add up to the balances"),
        # a line dropped, a line read twice, a sign flipped, an amount misread
        ({"lines": _lines()[:-1]}, "the lines don't add up to the balances"),
        ({"lines": [*_lines(), _lines()[0]]}, "the lines don't add up to the balances"),
        ({"lines": _lines((3, "amount", "1204.50"))}, "the lines don't add up to the balances"),
        ({"lines": _lines((3, "amount", "-1240.50"))}, "the lines don't add up to the balances"),
        # a fraction of a cent is a misreading, never rounded
        ({"lines": _lines((0, "amount", "250.004"))}, "a line with no amount in whole cents"),
        ({"closing_balance": "3374.740"}, "no opening or closing balance"),
        ({"lines": _lines((0, "amount", "0.00"))}, "a line with no amount in whole cents"),
        ({"lines": _lines((0, "amount", "2.5E+2"))}, "a line with no amount in whole cents"),
        # every line inside the statement's own period
        ({"lines": _lines((0, "day", "2026-07-31"))}, "a line outside the statement's period"),
        ({"lines": _lines((5, "day", "2026-10-01"))}, "a line outside the statement's period"),
        ({"lines": _lines((2, "day", "Aug 12"))}, "a line outside the statement's period"),
        ({"lines": _lines((2, "words", "  \t "))}, "a line with no description"),
        # the period itself
        ({"period_end": "2026-10-31"}, "a period that can't be a statement's"),
        ({"period_start": "2026-10-01"}, "no period"),
        ({"period_start": None}, "no period"),
        ({"period_start": "2025-01-01"}, "a period that can't be a statement's"),
        # the totals it prints, when it prints them
        ({"total_in": "250.01"}, "the lines in don't make the total in"),
        ({"total_out": "1875.25"}, "the lines out don't make the total out"),
        ({"total_out": "-1875.26"}, ""),  # a total printed with its sign is the same total
        # what it is, and in what money
        ({"currency": "NPR"}, "not in dollars"),
        ({"currency": None}, "not in dollars"),
        ({"doc_type": "receipt"}, sr.NOT_A_STATEMENT),
        ({"lines": []}, "no lines"),
        ({"opening_balance": None}, "no opening or closing balance"),
    ],
)
def test_a_reading_that_does_not_add_up_offers_nothing(over, why):
    proven, said = _check(**over)
    assert said == why
    assert (proven is None) == (why != "")


def test_totals_left_out_are_not_needed_but_the_balances_always_are():
    proven, why = _check(total_in=None, total_out=None)
    assert proven is not None and why == ""


def test_too_many_lines_is_refused_before_any_sum():
    many = [{"day": "2026-08-03", "words": "FEE", "amount": "-0.01"}] * (sr.MAX_LINES + 1)
    proven, why = _check(lines=many, closing_balance="4999.99", total_in=None, total_out=None)
    assert proven is None and why == "too many lines"


def test_cents_are_whole_or_nothing():
    assert sr.cents("1234.5") == 123450 and sr.cents("-0.01") == -1 and sr.cents("12") == 1200
    for bad in ("1,234.56", "$12.00", "12.345", "1e3", "", None, "NaN", "Infinity"):
        assert sr.cents(bad) is None


def test_the_eval_sets_bad_statement_never_gets_past_the_check():
    """The made-up statement whose printed lines don't add up (eval set
    ``statement_read``): read faithfully, code turns it away."""
    from core.llm.evals import load_job

    spec = load_job(sr.JOB)
    bad = [c for c in spec.cases if "add up" in c.note]
    assert bad, "the set carries a statement that does not add up"
    for case in bad:
        lines = [{**line, "words": "a line"} for line in case.expect["lines"]]
        reply = sr.StatementReply(**{**case.expect, "lines": lines})
        assert sr.check(reply, date(2026, 11, 18)) == (
            None,
            "the lines don't add up to the balances",
        )
    for case in spec.cases:  # and every honest one in the set is proven
        if case in bad or case.expect.get("doc_type") != "bank_statement":
            continue
        lines = [{**line, "words": "a line"} for line in case.expect["lines"]]
        reply = sr.StatementReply(**{**case.expect, "lines": lines})
        proven, why = sr.check(reply, date(2026, 11, 18))
        assert why == "" and proven is not None, case.name


# ---- the page ----------------------------------------------------------------------------


def _on(solo, monkeypatch, *replies: dict, cap: str = "") -> None:  # noqa: F811
    toml = Path(solo["root"]) / "living-water" / "tenant.toml"
    text = toml.read_text().replace("[llm.jobs]\n", '[llm.jobs]\nstatement_read = "fixture"\n', 1)
    if cap:
        text = re.sub(r"(?m)^monthly_usd = \d+$", f"monthly_usd = {cap}", text)
    toml.write_text(text)
    script = Path(solo["root"]).parent / "statement-replies.jsonl"
    script.write_text("".join(json.dumps(r) + "\n" for r in replies))
    monkeypatch.setenv("ENGINE_FIXTURE_REPLIES", str(script))


def _calls(solo) -> list[tuple[str, str]]:  # noqa: F811
    db = Path(solo["ledger_root"]) / DB_FILENAME
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT job_type, status FROM llm_calls ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _receipts(solo) -> list[str]:  # noqa: F811
    return sorted({p.name for p in Path(solo["root"]).parent.rglob("*_receipt_*") if p.is_file()})


def _tap(solo, desk, body, pick: str):  # noqa: F811
    raw = f"pick={pick}".encode()
    headers = {"X-Receipt-Reading": body["ask"]["token"], "Content-Length": str(len(raw))}
    return upload(
        desk,
        headers,
        io.BytesIO(raw).read,
        origin="https://tim.example.org",
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )


def test_with_the_reader_off_the_pile_page_is_byte_for_byte_what_it_was():
    """The catch-up page's bytes before card R2 (main 6e8e169), and since
    with each file's name sent for whose account it says (t-account-from-name)
    and the hint to switch the iPhone picker to List view (t-picker-list-view)."""
    import hashlib

    for args, digest in {
        (): "a3f0bf2bf68c88348dcaa777abafd7f3a1d9586c82c9883326aee7db5786ec8c",
        (("ministry", "Grace"),): (
            "9b6a9d973491920c092947f4ef2497a9a816e2f070e7f4aa307f35c7a19e670a"
        ),
    }.items():
        html = page("n", many=True, **dict(args))
        assert hashlib.sha256(html.encode()).hexdigest() == digest
        assert html == page("n", many=True, statements=False, **dict(args))
    assert "id=ask" in page("n", many=True, statements=True)
    assert page("n", statements=True) == page("n")  # one receipt's page never asks about one


@pytest.mark.parametrize("on", [False, True])
def test_off_or_with_nothing_to_say_a_pdf_goes_in_as_it_always_did(solo, monkeypatch, on):  # noqa: F811
    """No line in the file: no model is asked. A bare fixture tier: the call
    fails, is recorded, and the PDF is filed with the receipts, as before."""
    if on:
        _on(solo, monkeypatch)
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, PDF)
    assert (status, body) == (200, {"ok": True, "say": "Got it."})
    assert _receipts(solo) == ["2026-10-14_receipt_undated.pdf"]
    assert door.call("bank_statement", {})["statements"] == []
    assert _calls(solo) == ([("statement_read", "transport_failed")] if on else [])


def test_a_csv_never_reaches_the_reader(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading())
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, MINISTRY_CSV.encode())
    assert status == 200 and "whose account" in body["say"] and "ask" not in body
    assert _calls(solo) == []


def test_a_receipt_read_as_something_else_goes_in_as_a_receipt(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading(doc_type="other", lines=[]))
    _door, desk, token = _daniel(solo)
    assert _send(solo, desk, token, PDF) == (200, {"ok": True, "say": "Got it."})
    assert _receipts(solo) == ["2026-10-14_receipt_undated.pdf"]


def test_the_month_cap_stops_the_call_before_it_is_made(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading(), cap="0")
    _door, desk, token = _daniel(solo)
    assert _send(solo, desk, token, PDF) == (200, {"ok": True, "say": "Got it."})
    assert _calls(solo) == [("statement_read", "budget_refused")]


@pytest.mark.security("SEC-12")
def test_a_statement_that_adds_up_is_offered_and_kept_in_one_tap(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading())
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, PDF)
    assert status == 200 and body["say"] == (
        "Your statement reads 6 lines, August 1, 2026 to September 30, 2026, starting at "
        "$5,000.00 and ending at $3,374.74. The lines add up to both. Keep them?"
    )
    assert body["note"] == body["say"]
    assert [c["label"] for c in body["ask"]["choices"]] == ["Keep them", "No"]
    # nothing is held, filed, or written before her tap
    assert door.call("bank_statement", {})["statements"] == [] and _receipts(solo) == []
    assert _events(solo, st.LINE_EVENT) == []
    status, out, _ = _tap(solo, desk, body, "keep")
    assert status == 200 and out["say"] == (
        "Got your statement: 6 lines, Aug 3 to Sep 2. Tell Tim whose account it is: the "
        "ministry's, or your own."
    )
    # from here it is the CSV's path: whose account, then kept
    [held] = door.call("bank_statement", {})["statements"]
    assert held["lines"] == 6 and held["kind"] == "bank statement"
    kept = door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    assert kept["status"] == "kept", kept
    read = _events(solo, st.LINE_EVENT)
    assert len(read) == 6
    # the bank's CSV of the same statement is the same lines: never twice the money
    _send(solo, desk, token, MINISTRY_CSV.encode())
    [again] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": again["statement"], "account": "ministry"})
    assert len(_events(solo, st.LINE_EVENT)) == 6
    # and her tap is spent
    assert _tap(solo, desk, body, "keep")[0] in (404, 410)
    assert len(door.call("bank_statement", {})["statements"]) == 0


def test_her_no_keeps_nothing(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading())
    door, desk, token = _daniel(solo)
    _status, body = _send(solo, desk, token, PDF)
    status, out, _ = _tap(solo, desk, body, "no")
    assert status == 200 and out["say"] == "All right. I didn't keep any of it."
    assert door.call("bank_statement", {})["statements"] == [] and _receipts(solo) == []


def test_a_statement_that_does_not_add_up_asks_for_the_csv_and_keeps_nothing(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading(closing_balance="3374.75"))
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, PDF)
    assert status == 200 and "ask" not in body
    assert body["say"].startswith("I couldn't make that statement's lines add up")
    assert "Send the CSV file your bank's website exports" in body["say"]
    assert door.call("bank_statement", {})["statements"] == [] and _receipts(solo) == []
    assert _events(solo, st.LINE_EVENT) == []
    # the link still works for the CSV
    assert "whose account" in _send(solo, desk, token, MINISTRY_CSV.encode())[1]["say"]


def test_a_tap_from_another_site_is_refused(solo, monkeypatch):  # noqa: F811
    _on(solo, monkeypatch, _reading())
    door, desk, token = _daniel(solo)
    _status, body = _send(solo, desk, token, PDF)
    raw = b"pick=keep"
    status, _out, _ = upload(
        desk,
        {"X-Receipt-Reading": body["ask"]["token"], "Content-Length": "9", "Origin": "https://x.y"},
        io.BytesIO(raw).read,
        origin="https://tim.example.org",
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )
    assert status == 403 and door.call("bank_statement", {})["statements"] == []


def test_a_one_receipt_link_never_reads_a_statement(solo, monkeypatch):  # noqa: F811
    """Only a catch-up link is where a statement goes."""
    from core.tools.receipts import ReceiptDesk, take_photo

    _on(solo, monkeypatch, _reading())
    desk = ReceiptDesk()
    token = desk.issue("daniel-reyes", "Daniel Reyes", None, "a receipt")
    status, body = take_photo(
        desk,
        token,
        PDF,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )
    assert status == 200 and "ask" not in body and _calls(solo) == []


def test_the_words_are_plain():
    for line in sr.words().values():
        assert "—" not in line
        for word in ("model", "token", "ledger", "json", "parse", "statement_read"):
            assert word not in line.casefold()


def test_the_reader_is_named_only_and_a_document_job():
    from core.llm.policy import DOCUMENT_JOB_TYPES, NAMED_ONLY

    assert sr.JOB in NAMED_ONLY and sr.JOB in DOCUMENT_JOB_TYPES
    assert _held  # the held statements read the same as a CSV's (shared helper)
