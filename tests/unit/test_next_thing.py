"""The next thing (docs/screens/README.md, chunk 1, the owner 2026-11-16).

A missionary does not know what Tim can do and will never read the FAQ, so
Tim leads: code reads the person's books and names the single most useful
thing waiting on them, with what it takes and what does it. ``start_here``
says it first, and every answer through the door ends with it, or with
"Nothing's waiting on you." No model picks it.

Daniel (made up, as in test_door_my_words) runs Living Water alone and
approves his own; Linda, his board treasurer, looks over his month.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.agents.expenses import statements as st
from core.tools import next_thing
from tests.unit.test_atm_cash import ATM_CSV, _receipt_on
from tests.unit.test_bank_catch_up import _daniel, _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_ministry_lines import _door
from tests.unit.test_missionary_month import feed  # noqa: F401  (fixture)


def _no_cards(solo) -> None:  # noqa: F811
    """The fixture's three waiting cards gone: a new person's empty books."""
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute("DELETE FROM approval_queue")
    con.commit()
    con.close()


def _next(door) -> dict:
    return door.call("start_here", {})["next"]


# ---- what comes first ------------------------------------------------------------


def test_a_yes_waiting_on_them_comes_first(solo):  # noqa: F811
    first = _next(_door(solo, "daniel-reyes"))
    assert first["kind"] == "yes"
    assert first["tool"] == "waiting_cards"
    assert "waiting on your yes" in first["say"]
    assert "and 2 more" in first["say"]  # three cards wait on him; one is named
    assert first["takes"] == "about a minute"


def test_start_here_says_the_next_thing_before_anything_else(solo):  # noqa: F811
    said = _door(solo, "daniel-reyes").call("start_here", {})
    assert next(iter(said)) == "next"


def test_someone_nothing_waits_on_hears_so(solo):  # noqa: F811
    # Linda looks over Daniel's month and decides none of his cards.
    first = _next(_door(solo, "linda-park"))
    assert first == {"kind": "nothing", "say": "Nothing's waiting on you."}


def test_a_new_person_is_led_to_their_first_bank_statement(solo):  # noqa: F811
    _no_cards(solo)
    first = _next(_door(solo, "daniel-reyes"))
    assert first["kind"] == "first_statement"
    assert first["tool"] == "catch_up_link"
    assert "first bank statement" in first["say"]
    assert "about 5 minutes" == first["takes"]


def test_the_ministrys_statement_counts_for_whoever_keeps_its_books(solo):  # noqa: F811
    # Daniel looks over every month; Linda sent the ministry's September
    # statement. Nothing is asked of him: his first statement is in, and so
    # is last month's (the president of a small ministry, 2026-11-16).
    _no_cards(solo)
    payload = st.Line("2026-09-04", "MAPUTO WATER CO", -18000).payload("Linda Park", st.MINISTRY)
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute(
        "INSERT INTO events (idempotency_key, run_id, tenant, agent, event_type, payload_json, "
        "created_at) VALUES ('pg-line', (SELECT MAX(id) FROM runs), 'living-water', 'expenses', "
        "?, ?, '2026-10-01T00:00:00+00:00')",
        (st.LINE_EVENT, json.dumps(payload)),
    )
    con.commit()
    con.close()
    assert _next(_door(solo, "daniel-reyes"))["kind"] == "nothing"


def test_a_statement_sent_waits_on_whose_account_it_is(solo):  # noqa: F811
    _no_cards(solo)
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, ATM_CSV.encode())
    first = _next(door)
    assert first["kind"] == "whose_statement"
    assert first["tool"] == "bank_statement"
    assert "whose account" in first["say"]


def _cash_in_hand(solo, today: str):  # noqa: F811
    """Daniel's August statement kept, the Aug 3 withdrawal said as 40,000
    rupees and the Aug 20 one as 21,000, his door's clock set to ``today``."""
    _no_cards(solo)
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, ATM_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    bank = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    atms = sorted((b for b in bank if b["role"] == "cash drawn"), key=lambda b: b["date"])
    door.call("explain_bank_line", {"line": atms[0]["source"], "said": "40,000 rupees"})
    door.call("explain_bank_line", {"line": atms[1]["source"], "said": "21,000 rupees"})
    door.today = today
    return door


def test_cash_at_45_days_is_the_next_thing(solo, feed):  # noqa: F811
    door = _cash_in_hand(solo, "2026-09-17")  # Aug 3 + 45 days
    first = _next(door)
    assert first["kind"] == "cash"
    assert first["tool"] == "count_cash"
    assert "45 days" in first["say"] and "60 days" in first["say"]


def test_cash_short_of_45_days_is_not_yet(solo, feed):  # noqa: F811
    door = _cash_in_hand(solo, "2026-09-16")  # 44 days
    assert _next(door)["kind"] != "cash"


def test_a_month_ready_to_send_is_the_next_thing(solo):  # noqa: F811
    _no_cards(solo)
    door, desk, token = _daniel(solo)
    _receipt_on(door, desk, token, solo, "2026-09-07", "$30.00", JPEG + b"m")
    first = _next(door)
    assert first["kind"] == "month_ready"
    assert first["tool"] == "send_month_for_review"
    assert first["args"] == {"month": "2026-09"}
    assert "September is ready" in first["say"]
    # Sent, it waits on his own yes (he approves his own), and then on nothing
    # but his first statement.
    door.call("send_month_for_review", {"month": "2026-09"})
    then = _next(door)
    assert then["kind"] == "yes" and "September 2026" in then["say"]


# ---- every answer ends with it ---------------------------------------------------


def test_every_answer_through_the_door_ends_with_the_next_thing(solo):  # noqa: F811
    door = _door(solo, "daniel-reyes")
    for tool in ("deadlines", "waiting_cards", "month_report", "open_payables"):
        said = door.call(tool, {})
        assert list(said)[-1] == "next", tool
        assert said["next"]["kind"] == "yes", tool


def test_the_owners_own_use_carries_no_next_thing(solo):  # noqa: F811
    # No viewer: the owner's local tools answer for the whole tenant, as before.
    from core.tools import catalog

    tools = catalog.Tools("living-water", ledger_root=solo["ledger_root"])
    assert "next" not in tools.call("waiting_cards", {})


# ---- the words -------------------------------------------------------------------


def test_the_words_carry_no_machinery_and_no_em_dash():
    for key, words in next_thing.WORDS.items():
        assert "—" not in words, key
        for banned in ("card", "tool", "ledger", "tenant", "_", "#"):
            assert banned not in words.lower(), (key, banned)


def test_every_kind_is_in_the_words_file():
    assert set(next_thing.RANK) <= set(next_thing.WORDS)
