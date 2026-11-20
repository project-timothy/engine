"""Tim reads whose account a statement is from its file name (board
t-account-from-name, the owner's walkthrough of 2026-10-16).

Ruth had to say which statement was the ministry's and which was hers by
their dates, when the file names already said it ("Ministry checking", "My
own checking"). Code reads the name; Tim asks one question, "the ministry's
and yours, right?", and her one yes settles both. Nothing is kept on a name
alone, a name that says neither still asks, and the name itself never
reaches her AI (it is her phone's words, never Tim's instructions).

Daniel (made up, as in test_door_my_words) runs Living Water alone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.agents.expenses import statements as st
from core.tools import next_thing
from core.tools.receipts import page, take_photo, upload
from core.tools.statement_names import ask, side_of
from tests.unit.test_bank_catch_up import _daniel, _events
from tests.unit.test_catch_up import NOW
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_next_thing import _next, _no_cards
from tests.unit.test_statements import MINISTRY_CSV, OWN_CSV

LIVING_WATER = "Living Water Mozambique, Inc."


def _send(solo, desk, token, data: bytes, name: str):  # noqa: F811
    return take_photo(
        desk,
        token,
        data,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        now=NOW,
        name=name,
    )


def _both_named(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    assert _send(solo, desk, token, MINISTRY_CSV.encode(), "Ministry checking.csv")[0] == 200
    assert _send(solo, desk, token, OWN_CSV.encode(), "My own checking.csv")[0] == 200
    return door, desk, token


# ---- what a name says ------------------------------------------------------------


def test_a_name_that_says_the_ministry_reads_as_the_ministrys():
    for name in (
        "Ministry checking.csv",
        "ministry_checking_2026-08.csv",
        "Church account.csv",
        "organization-savings.CSV",
        "Living Water checking.csv",
        "LivingWater-Aug.csv",
    ):
        assert side_of(name, LIVING_WATER) == st.MINISTRY, name


def test_a_name_that_says_its_theirs_reads_as_their_own():
    for name in ("My own checking.csv", "mine.csv", "Personal savings.csv", "MyChecking.csv"):
        assert side_of(name, LIVING_WATER) == st.OWN, name


def test_a_name_that_says_neither_or_both_says_nothing():
    for name in (
        "statement_2026-08.csv",
        "Export.csv",
        "My ministry checking.csv",  # both: hers to say
        "ownership.csv",  # a word inside a word is no word
        "Mozambique.csv",  # part of the ministry's name only
        "",
    ):
        assert side_of(name, LIVING_WATER) == "", name


def test_a_ministry_name_with_a_word_of_hers_in_it_still_reads_as_the_ministrys():
    assert side_of("My Fathers House checking.csv", "My Father's House, Inc.") == st.MINISTRY


def test_the_one_question_fits_what_the_names_say():
    assert ask(1, 1) == "Going by the names, the ministry's and yours, right?"
    assert "the ministry's" in ask(1, 0) and "yours" not in ask(1, 0)
    assert "yours" in ask(0, 1) and "ministry" not in ask(0, 1)
    assert ask(0, 0) == ""


def test_the_words_carry_no_machinery_and_no_em_dash():
    from core.tools.statement_names import WORDS

    for key, words in WORDS.items():
        assert "—" not in words, key
        for banned in ("card", "tool", "ledger", "tenant", "_", "#", "file name"):
            assert banned not in words.lower(), (key, banned)


# ---- the page sends the name -----------------------------------------------------


def test_the_catch_up_page_sends_each_files_name():
    html = page("n", many=True)
    assert "X-Receipt-Name" in html and "encodeURIComponent(f.name" in html


def test_the_box_reads_the_name_the_page_sent(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    data = MINISTRY_CSV.encode()
    headers = {
        "Content-Length": str(len(data)),
        "X-Receipt-Ticket": token,
        "X-Receipt-Name": "Ministry%20checking.csv",
    }
    status, _body, _close = upload(
        desk,
        headers,
        lambda n: data[:n],
        origin="https://box.example",
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )
    assert status == 200
    [held] = door.call("bank_statement", {})["statements"]
    assert held["named"] == st.MINISTRY


# ---- one question, one yes -------------------------------------------------------


def test_names_that_say_it_ask_one_question_and_keep_nothing(solo):  # noqa: F811
    door, _desk, _token = _both_named(solo)
    listed = door.call("bank_statement", {})
    named = sorted(s["named"] for s in listed["statements"])
    assert named == [st.MINISTRY, st.OWN]
    assert "Going by the names, the ministry's and yours, right?" in listed["say"]
    assert "as_named" in listed["say"]
    assert _events(solo, st.LINE_EVENT) == []  # asking keeps nothing
    # Her phone's words never reach her AI: only what code read from them.
    assert "checking" not in json.dumps(listed).lower()


def test_her_one_yes_settles_both(solo):  # noqa: F811
    door, _desk, _token = _both_named(solo)
    said = door.call("bank_statement", {"as_named": True})
    assert said["status"] == "as_named", said
    kept = {a["account"]: a for a in said["answers"]}
    assert kept[st.MINISTRY]["status"] == "kept"
    assert kept[st.OWN]["status"] == "pick" and kept[st.OWN]["rows"]
    lines = _events(solo, st.LINE_EVENT)
    assert len(lines) == 6 and {ln["account"] for ln in lines} == {st.MINISTRY}
    # The own account waits on her picks, never on the question again.
    [left] = door.call("bank_statement", {})["statements"]
    assert "named" not in left


def test_a_no_falls_back_to_asking_whose_each_is(solo):  # noqa: F811
    door, _desk, _token = _both_named(solo)
    listed = door.call("bank_statement", {})
    assert "On a no" in listed["say"] and "whose account each" in listed["say"]
    own = next(s for s in listed["statements"] if s["named"] == st.OWN)
    # Her no: "that one's the ministry's too". Today's path, one by one.
    said = door.call("bank_statement", {"statement": own["statement"], "account": "ministry"})
    assert said["status"] == "kept"


def test_a_name_that_says_neither_still_asks(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_CSV.encode(), "statement_2026-08.csv")
    listed = door.call("bank_statement", {})
    [held] = listed["statements"]
    assert "named" not in held
    assert "Going by" not in listed["say"]
    assert "Ask them whose account each bank statement is" in listed["say"]
    said = door.call("bank_statement", {"as_named": True})
    assert said["status"] == "which"
    assert _events(solo, st.LINE_EVENT) == []
    assert door.call("bank_statement", {})["statements"] == listed["statements"]


def test_one_named_and_one_not_settles_only_the_named(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, MINISTRY_CSV.encode(), "Ministry checking.csv")
    _send(solo, desk, token, OWN_CSV.encode(), "Export.csv")
    listed = door.call("bank_statement", {})
    assert "Going by its name, that's the ministry's, right?" in listed["say"]
    assert "the others" in listed["say"]
    door.call("bank_statement", {"as_named": True})
    assert len(_events(solo, st.LINE_EVENT)) == 6
    [left] = door.call("bank_statement", {})["statements"]
    assert left["from"] == "2026-08-04" and "named" not in left


def test_a_reviewer_never_walks_someones_own_account_by_its_name(solo):  # noqa: F811
    _door_, desk, _token = _both_named(solo)
    linda = _door(solo, "linda-park")
    linda.receipts.desk = desk
    said = linda.call("bank_statement", {"person": "Daniel", "as_named": True})
    own = next(a for a in said["answers"] if a["account"] == st.OWN)
    assert own["status"] == "refused" and "rows" not in own


# ---- the next thing says the shorter question ------------------------------------


def test_the_next_thing_asks_the_one_question(solo):  # noqa: F811
    _no_cards(solo)
    door, _desk, _token = _both_named(solo)
    first = _next(door)
    assert first["kind"] == "whose_statement" and first["tool"] == "bank_statement"
    assert first["say"] == (
        "What you sent is waiting on one yes. Going by the names, the ministry's and yours, right?"
    )
    assert first["args"] == {}  # her yes comes first; the next thing never settles


def test_the_next_thing_asks_as_before_when_a_name_says_neither(solo):  # noqa: F811
    _no_cards(solo)
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_CSV.encode(), "Export.csv")
    assert _next(door)["say"] == next_thing.WORDS["whose_statement"]


def test_nothing_of_a_name_is_written_anywhere(solo):  # noqa: F811
    door, _desk, _token = _both_named(solo)
    door.call("bank_statement", {})
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    rows = con.execute("SELECT payload_json FROM events").fetchall()
    con.close()
    assert not any("checking" in r[0].lower() for r in rows)


def test_a_statement_tim_read_keeps_its_files_name_for_her_yes():
    from core.tools.receipts import STATEMENT, ReceiptDesk, Ticket, keep_statement

    desk = ReceiptDesk()
    ticket = Ticket("daniel-reyes", "Daniel Reyes", None, pile=True, by="daniel-reyes")
    rows = tuple(st.parse(MINISTRY_CSV))
    token = desk.propose(ticket, rows, "Ministry checking October.pdf")
    assert keep_statement(desk, token, "keep")[0] == 200
    [held] = desk.held("daniel-reyes")
    assert held.kind == STATEMENT and held.file_name == "Ministry checking October.pdf"


def test_the_name_header_is_its_last_part_and_never_long():
    from core.tools.receipts import file_name

    assert file_name({"X-Receipt-Name": "C%3A%5CUsers%5Cruth%5CMy%20own.csv"}) == "My own.csv"
    assert len(file_name({"X-Receipt-Name": "a" * 5000})) == 200
    assert file_name({}) == ""
