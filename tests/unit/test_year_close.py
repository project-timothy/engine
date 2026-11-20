"""The books close once a year, by calendar year (the owner, 2026-11-10:
"months are never sealed; the books close once a year"). Whoever looks over
every month asks for it once the year is over; the card carries the year's
numbers, the ones the 1099s and the 990 are taken from, and their yes closes
it. After that, nothing dated in that year changes: a late receipt is
offered this year instead, and a line of the closed year stays as it was."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.agents.expenses import statements as st
from core.agents.expenses import year_close as yc
from core.engine.runner import run
from tests.unit.test_bank_catch_up import _both
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_missionary_month import _cards, feed  # noqa: F401  (fixture)


def _db(solo) -> Path:  # noqa: F811
    return Path(solo["ledger_root"]) / "ledger.sqlite3"


def _close_cards(solo) -> list[dict]:  # noqa: F811
    con = sqlite3.connect(_db(solo))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ?",
        (yc.CLOSE_CARD,),
    ).fetchall()
    con.close()
    return [{"id": r["id"], "status": r["status"], **json.loads(r["params_json"])} for r in rows]


def _yes(solo, card_id: int, at: str = "2027-01-20T15:00:00+00:00") -> None:  # noqa: F811
    con = sqlite3.connect(_db(solo))
    con.execute(
        "UPDATE approval_queue SET status = 'approved', resolved_at = ? WHERE id = ?",
        (at, card_id),
    )
    con.commit()
    con.close()


def _linda(solo, today: str = "2027-01-20"):  # noqa: F811
    door = _door(solo, "linda-park")
    door.today = today
    return door


def _closed_2026(solo):  # noqa: F811
    """Daniel's August is in the books, then Linda closes 2026 in January."""
    daniel, _desk, _token = _both(solo)
    linda = _linda(solo)
    asked = linda.call("close_year", {"year": "2026", "said": "close the 2026 books"})
    assert asked["status"] == "waiting", asked
    [card] = _close_cards(solo)
    _yes(solo, card["id"])
    daniel.today = "2027-01-21"
    return daniel, linda


# ---- by code -----------------------------------------------------------------------


def test_a_year_is_closed_only_by_a_yes_on_its_card():
    cards = [
        {"status": "approved", "params": {"year": "2025"}, "resolved_at": "2026-01-20T15:00:00Z"},
        {"status": "pending", "params": {"year": "2026"}, "resolved_at": None},
        {"status": "rejected", "params": {"year": "2024"}, "resolved_at": "2025-01-03T15:00:00Z"},
    ]
    closed = yc.closed_years(cards)
    assert list(closed) == ["2025"]
    assert closed["2025"].on == "2026-01-20"
    assert yc.closed_for("2025-12-31", closed) is closed["2025"]
    assert yc.closed_for("2026-01-01", closed) is None
    assert yc.closed_for("", closed) is None


# ---- asking for it -----------------------------------------------------------------


def test_a_year_closes_only_once_it_is_over(solo):  # noqa: F811
    linda = _linda(solo, today="2026-12-31")
    said = linda.call("close_year", {"year": "2026", "said": "close the books"})
    assert said["status"] == "not_yet" and "2027" in said["say"], said
    assert _close_cards(solo) == []


def test_only_whoever_looks_over_every_month_asks_for_it(solo):  # noqa: F811
    _both(solo)
    linda = _linda(solo)
    assert "close_year" in linda._specs
    daniel = _door(solo, "daniel-reyes")
    daniel.today = "2027-01-20"
    if "close_year" in daniel._specs:  # a solo filer who sees every month may ask
        said = daniel.call("close_year", {"year": "2026", "said": "close 2026"})
        assert said["status"] in ("waiting", "refused")


def test_the_card_carries_the_years_numbers_and_what_still_waits(solo):  # noqa: F811
    _both(solo)
    linda = _linda(solo)
    books = linda.call("books", {"period": "2026"})
    asked = linda.call("close_year", {"year": "2026", "said": "close the 2026 books"})
    assert asked["status"] == "waiting", asked
    [card] = _close_cards(solo)
    assert card["year"] == "2026" and card["by"] == "linda-park"
    assert card["spent"] == books["spent"] and card["came_in"] == books["came_in"]
    assert json.loads(card["spent_by_kind"]) == books["spent_by_kind"]
    assert len(card["digest"]) == 64
    assert "2026" in card["what"] and f"${books['spent']}" in card["what"].replace(",", "")
    # The fixture's August and September cards still wait on a yes: the close
    # card says so and never refuses for it.
    assert "August" in card["what"] and "September" in card["what"]
    assert "decide_card" in asked["say"] and str(card["id"]) in asked["say"]


def test_asking_twice_is_the_same_card(solo):  # noqa: F811
    _both(solo)
    linda = _linda(solo)
    linda.call("close_year", {"year": "2026", "said": "close 2026"})
    again = linda.call("close_year", {"year": "2026", "said": "close 2026 please"})
    assert again["status"] == "waiting"
    assert len(_close_cards(solo)) == 1


def test_a_closed_year_says_so_when_asked_again(solo):  # noqa: F811
    _daniel, linda = _closed_2026(solo)
    said = linda.call("close_year", {"year": "2026", "said": "close 2026"})
    assert said["status"] == "closed" and "January 20, 2027" in said["say"], said


# ---- once it is closed ---------------------------------------------------------------


def _house(door) -> dict:
    bank = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    return next(b for b in bank if b["words"] == "KATHMANDU GUEST HOUSE")


def test_a_split_of_a_closed_years_line_is_refused_in_a_plain_sentence(solo):  # noqa: F811
    daniel, _linda_door = _closed_2026(solo)
    house = _house(daniel)
    said = daniel.call(
        "split_line", {"line": house["source"], "said": "130 of that was mine", "whose": "own"}
    )
    assert said["status"] == "year_closed", said
    assert "2026" in said["say"] and "closed" in said["say"]
    assert _house(daniel)["dollars"] == house["dollars"]  # the line stays as it was
    explained = daniel.call(
        "explain_bank_line", {"line": house["source"], "said": "that was lodging"}
    )
    assert explained["status"] == "year_closed"


def test_a_late_receipt_for_a_closed_year_is_offered_this_year(solo):  # noqa: F811
    daniel, _linda_door = _closed_2026(solo)
    said = daniel.call(
        "set_receipt_day",
        {"line": "events#999999", "day": "2026-12-20", "said": "the 20th of December"},
    )
    assert said["status"] == "year_closed", said
    assert "2027" in said["say"] and "2027-01-21" in said["say"], said


def test_a_month_of_a_closed_year_is_refused_for_review(solo):  # noqa: F811
    daniel, _linda_door = _closed_2026(solo)
    said = daniel.call("send_month_for_review", {"month": "2026-12"})
    assert said["status"] == "year_closed", said


def test_the_open_year_still_takes_its_changes(solo):  # noqa: F811
    daniel, _linda_door = _closed_2026(solo)
    said = daniel.call("set_receipt_day", {"line": "events#999999", "day": "2027-01-05"})
    assert said["status"] != "year_closed"


def test_reading_the_closed_year_still_answers(solo):  # noqa: F811
    _daniel, linda = _closed_2026(solo)
    books = linda.call("books", {"period": "2026"})
    [card] = _close_cards(solo)
    assert books["spent"] == card["spent"]


def test_a_statement_line_of_a_closed_year_is_left_out(solo):  # noqa: F811
    _closed_2026(solo)
    lines = [
        {"day": "2026-12-28", "words": "LATE PRINTING", "cents": -4218, "n": 0},
        {"day": "2027-01-04", "words": "JANUARY PRINTING", "cents": -1500, "n": 0},
    ]
    done = run(
        "living-water",
        "expenses",
        "statement",
        params={
            "person": "Daniel Reyes",
            "account": st.MINISTRY,
            "lines": json.dumps(lines),
            "by": "daniel-reyes",
        },
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )
    assert "left out" in done.summary and "2026" in done.summary, done.summary
    con = sqlite3.connect(_db(solo))
    words = [
        json.loads(p)["words"]
        for (p,) in con.execute(
            "SELECT payload_json FROM events WHERE event_type = ?", (st.LINE_EVENT,)
        )
    ]
    con.close()
    assert "JANUARY PRINTING" in words and "LATE PRINTING" not in words


def test_a_closed_month_raises_no_new_card(solo):  # noqa: F811
    _closed_2026(solo)
    before = _cards(solo, "2026-08")
    run(
        "living-water",
        "expenses",
        "month",
        params={"month": "2026-08"},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )
    after = _cards(solo, "2026-08")
    assert [(c["id"], c["status"]) for c in after] == [(c["id"], c["status"]) for c in before]


def test_the_person_hears_which_statement_lines_a_closed_year_left_out(solo):  # noqa: F811
    from core.ledger.ledger import Ledger
    from tests.unit.test_bank_catch_up import _daniel, _held
    from tests.unit.test_catch_up import _send
    from tests.unit.test_statements import MINISTRY_CSV

    with Ledger.open(solo["ledger_root"]) as ledger:
        run_id = ledger.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        ledger.enqueue_approval(
            idempotency_key="closeyear:2026:test",
            run_id=run_id,
            tenant="living-water",
            agent="expenses",
            action_type=yc.CLOSE_CARD,
            params={"year": "2026"},
        )
    [card] = _close_cards(solo)
    _yes(solo, card["id"])
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, MINISTRY_CSV.encode())
    held = _held(door)["2026-08-03"]
    kept = door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    assert "6 were dated in the closed 2026 books and left out" in kept["say"], kept
