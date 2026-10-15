"""Bank statements as the backbone of a catch-up, through the door (board
t-statements, chunk 2, the owner's decisions of 2026-10-14).

Daniel sends two made-up statements through his catch-up link: the
ministry's account, where every line is the ministry's, and his own, where
only the lines he picks ever reach the books. Money that moved between
pockets is never counted twice, receipts stand behind their lines (a card's
dollars beat the day's rate), his school list adds the scholarships that
have no receipt, and August goes to his reviewer as one card."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.agents.expenses import statements as st
from core.tools.receipts import ReceiptDesk
from tests.unit.test_catch_up import NOW, _link, _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_expense_records import _approve
from tests.unit.test_ministry_lines import _door
from tests.unit.test_missionary_month import _cards, _month, feed  # noqa: F401
from tests.unit.test_statements import MINISTRY_CSV, OWN_CSV, SCHOOL_CSV

NPR_AUG_7 = "136.0"
NPR_AUG_18 = "138.5"


def _events(solo, kind: str) -> list[dict]:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    rows = con.execute(
        "SELECT payload_json FROM events WHERE event_type = ? ORDER BY id", (kind,)
    ).fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]


def _everything(solo) -> str:  # noqa: F811
    """Every byte the box holds for the ministry: ledger, event log, drop tree."""
    out = []
    for path in Path(solo["root"]).parent.rglob("*"):
        if path.is_file():
            out.append(path.read_bytes().decode("latin-1"))
    return "\n".join(out)


def _daniel(solo):  # noqa: F811
    desk = ReceiptDesk()
    door = _door(solo, "daniel-reyes")
    door.receipts.desk = desk
    token = _link(door.call("catch_up_link", {})).split("#", 1)[1]
    return door, desk, token


def _held(door) -> dict[str, dict]:
    """The held statements by their first day."""
    return {s["from"]: s for s in door.call("bank_statement", {})["statements"]}


def _both(solo):  # noqa: F811
    """Both statements sent, the ministry's kept, the own one's picks made."""
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, MINISTRY_CSV.encode())
    _send(solo, desk, token, OWN_CSV.encode())
    held = _held(door)
    ministry, own = held["2026-08-03"], held["2026-08-04"]
    door.call("bank_statement", {"statement": ministry["statement"], "account": "ministry"})
    walked = door.call("bank_statement", {"statement": own["statement"], "account": "own"})
    by_words = {r["words"]: r["row"] for r in walked["rows"]}
    picked = [
        by_words["BHATBHATENI SUPERMARKET KATHMANDU"],
        by_words["TAXI KATHMANDU"],
        by_words["REMITLY"],
    ]
    kept = door.call(
        "keep_own_lines",
        {"statement": own["statement"], "rows": picked, "said": "those three are the ministry's"},
    )
    assert kept["status"] == "kept", kept
    return door, desk, token


def test_a_statement_through_the_pile_link_is_held_never_written(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, OWN_CSV.encode())
    assert status == 200, body
    assert "whose account" in body["say"]
    assert _events(solo, st.LINE_EVENT) == []
    assert "NETFLIX" not in _everything(solo)
    [held] = door.call("bank_statement", {})["statements"]
    assert held["lines"] == 6 and held["from"] == "2026-08-04" and held["to"] == "2026-08-21"
    assert held["kind"] == "bank statement"


def test_a_file_that_isnt_a_statement_is_refused_and_the_link_still_works(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, b"just some words, nothing more\nno money\n")
    assert status == 415 and "statement" in body["say"]
    assert _send(solo, desk, token, JPEG)[0] == 200


def test_the_ministrys_account_keeps_every_line(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, MINISTRY_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    said = door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    assert said["status"] == "kept", said
    assert len(_events(solo, st.LINE_EVENT)) == 6
    assert door.call("bank_statement", {})["statements"] == []
    # The same statement sent again is the same lines, never twice the money.
    _send(solo, desk, token, MINISTRY_CSV.encode())
    [again] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": again["statement"], "account": "ministry"})
    assert len(_events(solo, st.LINE_EVENT)) == 6


def test_the_persons_own_lines_are_proposed_picked_and_the_rest_never_kept(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    walked = door.call("bank_statement", {"statement": held["statement"], "account": "own"})
    looks = {r["words"]: r["looks"] for r in walked["rows"]}
    assert looks["TAXI KATHMANDU"] == "Travel" and looks["NETFLIX.COM"] == ""
    assert "never say" in walked["say"]
    assert _events(solo, st.LINE_EVENT) == []  # walking keeps nothing
    taxi = next(r["row"] for r in walked["rows"] if r["words"] == "TAXI KATHMANDU")
    door.call("keep_own_lines", {"statement": held["statement"], "rows": [taxi], "said": "taxi"})
    [kept] = _events(solo, st.LINE_EVENT)
    assert kept["words"] == "TAXI KATHMANDU" and kept["account"] == st.OWN
    assert door.call("bank_statement", {})["statements"] == []
    box = _everything(solo)
    assert "NETFLIX" not in box and "PAYROLL" not in box and "2,400" not in box


def test_a_reviewer_never_walks_someones_own_account(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_CSV.encode())
    linda = _door(solo, "linda-park")
    linda.receipts.desk = desk
    [held] = linda.call("bank_statement", {"person": "Daniel"})["statements"]
    refused = linda.call(
        "bank_statement",
        {"person": "Daniel", "statement": held["statement"], "account": "own"},
    )
    assert refused["status"] == "refused"
    assert "rows" not in refused


def test_money_that_moved_is_never_spending(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    [paid] = _events(solo, st.TRANSFER_EVENT)
    assert paid["day"] == "2026-08-12"
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    roles = {b["words"]: b["role"] for b in month["bank"]}
    assert roles["ONLINE TRANSFER TO CHK ...9921"] == "paid back"
    assert roles["REMITLY* SEND TO NEPAL"] == "sent abroad"
    assert roles["REMITLY"] == "sent abroad"
    assert roles["ONLINE GIFT HOLLIS FAMILY"] == "came in"
    assert roles["KATHMANDU GUEST HOUSE"] == "the ministry paid"
    assert roles["TAXI KATHMANDU"] == "owed back"
    assert all("source" in b and b["source"].startswith("bank#") for b in month["bank"])


def test_a_line_with_no_receipt_asks_for_one_or_what_it_was_for(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    waiting = {b["words"]: b["waiting_on"] for b in month["bank"]}
    assert waiting["KATHMANDU GUEST HOUSE"] == "its receipt, or what it was for"
    assert waiting["TAXI KATHMANDU"] is None  # Tim's Travel, picked by Daniel
    guest = next(b for b in month["bank"] if b["words"] == "KATHMANDU GUEST HOUSE")
    sent = door.call(
        "send_month_for_review", {"month": "2026-08"}
    )  # a line still waits: nothing goes
    assert sent["status"] == "waiting"
    said = door.call(
        "explain_bank_line", {"line": guest["source"], "said": "the team's hotel in Kathmandu"}
    )
    assert said["status"] == "set", said
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    guest = next(b for b in month["bank"] if b["words"] == "KATHMANDU GUEST HOUSE")
    assert guest["waiting_on"] is None and guest["category"] == "Lodging"


def test_a_rupee_receipt_rides_its_card_line_at_the_cards_dollars(solo, feed):  # noqa: F811
    feed("2026-08-07", npr=NPR_AUG_7)
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _both(solo)
    _send(solo, desk, token, JPEG + b"bhat", at=NOW)
    [line] = door.call("month_report", {"month": "2026-10"})["people"][0]["lines"]
    door.call("set_receipt_day", {"line": line["source"], "said": "Aug 7", "day": "2026-08-07"})
    [line] = door.call("month_report", {"month": "2026-08"})["people"][0]["lines"]
    door.call("set_amount", {"line": line["source"], "said": "4,300 rupees"})
    # The month job records the day's rate; then the receipt finds its line.
    _month(solo, "2026-08")
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    bhat = next(b for b in month["bank"] if b["words"].startswith("BHATBHATENI"))
    assert bhat["dollars"] == "31.62" and bhat["receipt"] == "attached"
    assert month["lines"] == []  # the receipt rides its line, never a line of its own


def test_the_school_list_adds_scholarships_with_no_receipt(solo, feed):  # noqa: F811
    feed("2026-08-18", npr=NPR_AUG_18)
    door, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, SCHOOL_CSV.encode())
    assert status == 200 and "school list" in body["say"]
    [held] = door.call("bank_statement", {})["statements"]
    assert held["kind"] == "school list" and held["lines"] == 2
    said = door.call("keep_school_list", {"statement": held["statement"], "said": "add them"})
    assert said["status"] == "kept", said
    _month(solo, "2026-08")
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    school = {b["words"]: b for b in month["bank"]}
    assert school["Bikash Tamang, Shree Janata School"]["dollars"] == "85.00"
    asha = school["Asha Gurung, Shree Janata School"]
    assert asha["dollars"] == "86.64" and asha["said"] == "NPR 12,000.00"
    assert asha["waiting_on"] is None and asha["role"] == "owed back"


def test_august_goes_to_the_reviewer_as_one_card_owing_only_what_daniel_paid(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    for b in door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]:
        if b["waiting_on"]:
            door.call("explain_bank_line", {"line": b["source"], "said": "printing for the school"})
    sent = door.call("send_month_for_review", {"month": "2026-08"})
    assert sent["status"] == "sent", sent
    [card] = [c for c in _cards(solo, "2026-08") if "lines" in c]  # the fixture seeds one
    assert card["total_cents"] == "3842"  # the supermarket and the taxi; Remitly only moved
    lines = card["lines"].split("; ")
    assert len(lines) == 3  # the supermarket, the taxi, the guest house
    assert [ln for ln in lines if ln.endswith("the ministry paid")] == [
        "2026-08-20: $1,204.50, KATHMANDU GUEST HOUSE, Ministry Supplies, the ministry paid"
    ]
    assert "$500.00 sent abroad" in card["moved"] and "$86.40 paid back" in card["moved"]
    _approve(solo, {"id": card["id"]}, at="2099-01-01T00:00:00+00:00")  # after every fact
    _month(solo, "2026-08")
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    filed = con.execute("SELECT amount_cents, note FROM expense_line").fetchall()
    con.close()
    # Only what the ministry owes Daniel; the guest house was the ministry's own.
    assert sorted(filed) == [(680, "TAXI KATHMANDU"), (3162, "BHATBHATENI SUPERMARKET KATHMANDU")]
    # Built, each line still reads once, as a bank line, and counts once.
    [month] = door.call("month_report", {"month": "2026-08"})["people"]
    assert month["lines"] == [] and month["total"] == "38.42"
    taxi = next(b for b in month["bank"] if b["words"] == "TAXI KATHMANDU")
    assert taxi["stands"] == "reported"


def test_wheres_my_year_counts_the_bank_lines(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    [me] = door.call("year_report", {"year": "2026"})["people"]
    august, september = me["months"]
    assert august["month"] == "2026-08" and august["bank_lines"] == 7
    assert august["waiting"] == 2  # the guest house, and the supermarket nobody explained
    assert september["bank_lines"] == 2 and september["waiting"] == 2  # two Staples lines


def test_the_approve_page_says_what_only_moved(solo):  # noqa: F811
    from core.tools import decide

    door, _desk, _token = _both(solo)
    for b in door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]:
        if b["waiting_on"]:
            door.call("explain_bank_line", {"line": b["source"], "said": "printing"})
    door.call("send_month_for_review", {"month": "2026-08"})
    [card] = [c for c in _cards(solo, "2026-08") if "lines" in c]
    row = {"agent": "expenses", "action_type": "expenses.report_review", "params": card}
    words = decide.plain_words(row, "approve")
    assert "Aug 20: $1,204.50, KATHMANDU GUEST HOUSE" in words
    assert "Not spending: Aug 3: $250.00 came in" in words
    assert "Aug 5: $500.00 sent abroad" in words


def test_the_pile_page_takes_a_csv_and_shows_what_a_statement_needs():
    from core.tools.receipts import page

    html = page("n", many=True)
    assert ".csv" in html and "notes.push(d.note)" in html
    assert "notes.join('\\n\\n')" in html  # an escape in the script, never a raw line break


def test_a_statement_reply_carries_its_note_for_the_page(solo):  # noqa: F811
    _door_, desk, token = _daniel(solo)
    status, body = _send(solo, desk, token, MINISTRY_CSV.encode())
    assert status == 200 and body["note"] == body["say"]
    assert _send(solo, desk, token, JPEG)[1].get("note") is None  # a receipt has none
