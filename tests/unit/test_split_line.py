"""A line split across kinds, and money sent abroad that was the person's own
(the owner 2026-10-29, Caleb's 2026 books: 513,000 rupees of cash went 30% to
pay, 25% to teaching, 25% to the children's home, 15% to scholarships and 5%
to outreach; a $1,791.18 Remitly was medicine for himself).

A split never changes what a line costs: its parts add up to the cent, each
under its own kind. A send on the ministry's account that was their own is
owed to the ministry, never sent abroad for it."""

from __future__ import annotations

from pathlib import Path

from core.agents.expenses import statements as st
from core.agents.expenses.records import CASH_USED_EVENT, LINE_PURPOSE_EVENT, LINE_SPLIT_EVENT
from tests.unit.test_atm_cash import PERSON
from tests.unit.test_bank_catch_up import _both, _events
from tests.unit.test_cash_count import _drawn, _used
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_missionary_month import feed  # noqa: F401  (fixture)

CATEGORIES = ["Pay", "Teaching", "Children's Home", "Scholarships", "Outreach", "Other"]
CALEB = [("Pay", 30), ("Teaching", 25), ("Children's Home", 25), ("Scholarships", 15)]


def _line(day: str, words: str, cents: int) -> dict:
    return {
        "event_type": st.LINE_EVENT,
        "payload": st.Line(day, words, cents).payload(PERSON, st.MINISTRY),
    }


def _split(sha: str, parts: list[tuple[str, int]]) -> dict:
    payload = {"sha256": sha, "parts": [{"category": c, "percent": p} for c, p in parts]}
    return {"event_type": LINE_SPLIT_EVENT, "payload": payload}


def _take(sha: str, tag: str, category: str = "", whose: str = "ministry") -> dict:
    payload = {"sha256": sha, "take": tag, "category": category, "whose": whose, "said": "x"}
    return {"event_type": LINE_SPLIT_EVENT, "payload": payload}


def _purpose(sha: str, **said) -> dict:
    return {"event_type": LINE_PURPOSE_EVENT, "payload": {"sha256": sha, "said": "x", **said}}


# ---- by code -----------------------------------------------------------------------


def test_the_parts_add_up_to_the_cent():
    assert st.shares(10001, [30, 25, 25, 15, 5]) == [3001, 2500, 2500, 1500, 500]
    assert sum(st.shares(343342, [30, 25, 25, 15, 5])) == 343342
    assert st.shares(100, [33, 33, 34]) == [33, 33, 34]


def test_cash_with_no_receipt_splits_across_kinds():
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR4000.00", "ministry", "the region")]
    log.append(_split("u1", [*CALEB, ("Outreach", 5)]))
    rows = [
        ln for ln in st.month_lines(log, "2026-08", categories=CATEGORIES) if ln.get("cash_used")
    ]
    assert [(r["category"], r["cents"]) for r in rows] == [
        ("Pay", 900),
        ("Teaching", 750),
        ("Children's Home", 750),
        ("Scholarships", 450),
        ("Outreach", 150),
    ]
    assert {r["part"] for r in rows} == {"30%", "25%", "15%", "5%"}
    assert {r["sha256"] for r in rows} == {"u1"}  # each part is still the one line


def test_a_card_line_splits_and_the_newest_split_wins():
    line = _line("2026-08-20", "KATHMANDU GUEST HOUSE", -120450)
    sha = line["payload"]["sha256"]
    log = [line, _split(sha, [("Pay", 50), ("Other", 50)]), _split(sha, [("Teaching", 100)])]
    [row] = st.month_lines(log, "2026-08", categories=CATEGORIES)
    assert (row["category"], row["cents"]) == ("Teaching", 120450)


# ---- one way to split: an amount out of a line (the owner, 2026-11-10) -----------


def _house(*more: dict) -> list[dict]:
    line = _line("2026-08-20", "KATHMANDU GUEST HOUSE", -120450)
    sha = line["payload"]["sha256"]
    return [line, _purpose(sha, category="Lodging"), *(m(sha) for m in more)]


def test_an_amount_taken_out_leaves_the_rest_where_it_was():
    log = _house(lambda sha: _take(sha, "$300.00", "Meals"))
    rows = st.month_lines(log, "2026-08", categories=CATEGORIES + ["Lodging", "Meals"])
    assert [(r["category"], r["cents"], r["part_no"]) for r in rows] == [
        ("Lodging", 90450, 1),
        ("Meals", 30000, 2),
    ]
    assert [r["part"] for r in rows] == ["$904.50", "$300.00"]


def test_the_same_kind_said_again_replaces_its_amount():
    log = _house(
        lambda sha: _take(sha, "$300.00", "Meals"),
        lambda sha: _take(sha, "$100.00", "Teaching"),
        lambda sha: _take(sha, "$200.00", "Meals"),
    )
    rows = st.month_lines(log, "2026-08", categories=CATEGORIES + ["Lodging", "Meals"])
    assert [(r["category"], r["cents"]) for r in rows] == [
        ("Lodging", 90450),
        ("Teaching", 10000),
        ("Meals", 20000),
    ]


def test_their_own_part_of_a_ministry_line_is_owed_to_the_ministry():
    log = _house(lambda sha: _take(sha, "$130.00", whose="own"))
    rows = st.month_lines(log, "2026-08", categories=CATEGORIES + ["Lodging"])
    assert [(r["role"], r["cents"]) for r in rows] == [
        (st.MINISTRY_PAID, 107450),
        (st.REPAY, 13000),
    ]
    assert "$130.00 owed to the ministry" in st.repay_words(rows)


def test_rupees_taken_out_of_cash_carry_their_share_of_the_dollars():
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR4000.00", "ministry", "the region")]
    log.append(_take("u1", "NPR1000.00", "Scholarships"))
    rows = [
        ln for ln in st.month_lines(log, "2026-08", categories=CATEGORIES) if ln.get("cash_used")
    ]
    assert [(r["category"], r["cents"], r["said_amount"]) for r in rows] == [
        ("Other", 2250, "3000.00"),
        ("Scholarships", 750, "1000.00"),
    ]


def test_an_old_split_by_percent_still_reads_under_a_new_amount():
    """Caleb's 2026 books carry splits by percent; an amount said later comes
    off the whole line first and the percents divide what is left."""
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR4000.00", "ministry", "the region")]
    log += [_split("u1", [("Pay", 50), ("Teaching", 50)]), _take("u1", "NPR2000.00", "Outreach")]
    rows = [
        ln for ln in st.month_lines(log, "2026-08", categories=CATEGORIES) if ln.get("cash_used")
    ]
    assert [(r["category"], r["cents"]) for r in rows] == [
        ("Pay", 750),
        ("Teaching", 750),
        ("Outreach", 1500),
    ]


def test_amounts_that_take_the_whole_line_leave_the_old_percents_nothing():
    """An old percent split whose every part is said again by amount (Caleb's
    30% pay, now his own, 2026-11-12): the percents divide nothing, so they
    read as no line at all, never a row of $0.00; each kind keeps its dollars."""
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR4000.00", "ministry", "the region")]
    log.append(_split("u1", [*CALEB, ("Outreach", 5)]))
    log += [
        _take("u1", "NPR1200.00", whose="own"),
        _take("u1", "NPR1000.00", "Teaching"),
        _take("u1", "NPR1000.00", "Children's Home"),
        _take("u1", "NPR600.00", "Scholarships"),
        _take("u1", "NPR200.00", "Outreach"),
    ]
    rows = [
        ln for ln in st.month_lines(log, "2026-08", categories=CATEGORIES) if ln.get("cash_used")
    ]
    assert [(r["role"], r["category"], r["cents"]) for r in rows] == [
        (st.REPAY, "", 900),
        (st.MINISTRY_PAID, "Teaching", 750),
        (st.MINISTRY_PAID, "Children's Home", 750),
        (st.MINISTRY_PAID, "Scholarships", 450),
        (st.MINISTRY_PAID, "Outreach", 150),
    ]


def test_a_send_that_was_their_own_is_owed_to_the_ministry():
    send = _line("2026-09-08", "RMTLY* E7AFC REMITLY.COM WA 09/07", -179118)
    sha = send["payload"]["sha256"]
    [before] = st.month_lines([send], "2026-09")
    assert before["role"] == st.SENT
    [row] = st.month_lines([send, _purpose(sha, whose="own")], "2026-09")
    assert (row["role"], row["cents"]) == (st.REPAY, 179118)
    assert "$1,791.18 owed to the ministry" in st.repay_words([row])


# ---- through the door ----------------------------------------------------------------


def _bank(door, month: str) -> list[dict]:
    return door.call("month_report", {"month": month})["people"][0]["bank"]


def _house_row(door) -> dict:
    return next(b for b in _bank(door, "2026-08") if b["words"] == "KATHMANDU GUEST HOUSE")


def test_an_amount_comes_out_of_a_line_through_the_door(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    house = _house_row(door)
    whole = house["dollars"]
    said = door.call(
        "split_line",
        {
            "line": house["source"],
            "said": "300 of that was the training room",
            "category": "ministry supplies",
        },
    )
    assert said["status"] == "set", said
    assert "$300.00" in said["say"] and "Ministry Supplies" in said["say"]
    parts = [b for b in _bank(door, "2026-08") if b["words"] == "KATHMANDU GUEST HOUSE"]
    assert [p["dollars"] for p in parts] == [f"{float(whole) - 300:.2f}", "300.00"]
    assert parts[1]["category"] == "Ministry Supplies"
    [me] = door.call("year_report", {"year": "2026"})["people"]
    assert me["by_category"]["Ministry Supplies"] == "300.00"


def test_their_own_part_comes_out_through_the_door(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    house = _house_row(door)
    said = door.call(
        "split_line", {"line": house["source"], "said": "130 of that was mine", "whose": "own"}
    )
    assert said["status"] == "set", said
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    assert me["owed_to_ministry"] == "130.00"


def test_an_amount_that_isnt_a_part_or_names_no_kind_is_asked_again(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    house = _house_row(door)
    too_much = door.call(
        "split_line",
        {"line": house["source"], "said": "5,000 of that was meals", "category": "Meals"},
    )
    assert too_much["status"] == "asks" and "$1,204.50" in too_much["say"], too_much
    no_number = door.call(
        "split_line", {"line": house["source"], "said": "most of it was meals", "category": "Meals"}
    )
    assert no_number["status"] == "asks", no_number
    unknown = door.call(
        "split_line", {"line": house["source"], "said": "300 was rockets", "category": "Rockets"}
    )
    assert unknown["status"] == "needs_category" and "Lodging" in unknown["say"]


def test_a_split_by_percent_is_no_longer_taken(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    house = _house_row(door)
    said = door.call(
        "split_line",
        {"line": house["source"], "said": "60% lodging, 40% the training room"},
    )
    assert said["status"] == "asks" and "amount" in said["say"], said


def test_the_last_part_may_take_what_old_percents_still_hold(solo):  # noqa: F811
    """Caleb's cash line, split by percent in October, said again by amount
    (2026-11-12): the last part takes exactly what is left, so nothing is
    left for the old percents to divide."""
    from core.ledger import Ledger

    door, _desk, _token = _both(solo)
    house = _house_row(door)
    sha = next(e for e in _events(solo, st.LINE_EVENT) if e["words"] == house["words"])["sha256"]
    with Ledger.open(Path(solo["ledger_root"])) as ledger:
        [(run,)] = ledger.conn.execute("SELECT id FROM runs WHERE idempotency_key = 'seed-run'")
        assert ledger.append_event(
            idempotency_key="old-percent-split",
            run_id=run,
            tenant="living-water",
            agent="expenses",
            event_type=LINE_SPLIT_EVENT,
            payload={
                "sha256": sha,
                "parts": [
                    {"category": "Lodging", "percent": 60},
                    {"category": "Ministry Supplies", "percent": 40},
                ],
            },
        )
    mine = door.call(
        "split_line", {"line": house["source"], "said": "722.70 was mine", "whose": "own"}
    )
    assert mine["status"] == "set", mine
    rest = door.call(
        "split_line",
        {"line": house["source"], "said": "481.80 was supplies", "category": "Ministry Supplies"},
    )
    assert rest["status"] == "set", rest
    parts = [b for b in _bank(door, "2026-08") if b["words"] == house["words"]]
    assert [(p["role"], p["dollars"]) for p in parts] == [
        ("owed to the ministry", "722.70"),
        ("the ministry paid", "481.80"),
    ]


def test_the_remitly_that_was_theirs_through_the_door(solo):  # noqa: F811
    door, _desk, _token = _both(solo)
    send = next(b for b in _bank(door, "2026-08") if b["role"] == "sent abroad")
    said = door.call(
        "explain_bank_line",
        {"line": send["source"], "said": "that was medicine for me", "whose": "own"},
    )
    assert said["status"] == "set", said
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    assert me["owed_to_ministry"] == "500.00"


def test_events_name_their_kinds():
    assert LINE_SPLIT_EVENT == "bank.line_split" and CASH_USED_EVENT == "bank.cash_used"


# ---- the month card and the report price a split once (review, 2026-11-10) ---------
#
# The door read a split right while the card and the report priced every
# piece at the whole line: $30 of cash split five ways showed five $30 lines,
# and a person's own cash was owed back five times over.


def _card_items(log: list[dict]) -> list[dict]:
    import sqlite3
    from types import SimpleNamespace

    from core.agents.expenses import report_lines

    conn = sqlite3.connect(":memory:")
    conn.execute("create table expense_line (tenant, receipt_sha256)")
    ctx = SimpleNamespace(
        tenant_slug="t",
        ledger=SimpleNamespace(read_event_log=lambda: log, conn=conn),
        tenant=SimpleNamespace(
            expenses=SimpleNamespace(category_accounts=dict.fromkeys(CATEGORIES, "1"))
        ),
    )
    priced, _events = report_lines._price_claims(ctx, report_lines._bank_items(ctx, "2026-08"))
    return priced


def _five_ways(ministry: bool) -> list[dict]:
    whose = "ministry"
    log = [*_drawn(ministry), _used("u1", "2026-08-10", "NPR4000.00", whose, "the region")]
    log.append(_split("u1", [*CALEB, ("Outreach", 5)]))
    return _card_items(log)


def test_the_card_prices_each_part_of_a_split_at_its_share():
    from core.agents.expenses import report_lines

    items = _five_ways(ministry=True)
    assert [(i["category"], i["amount_cents"]) for i in items] == [
        ("Pay", 900),
        ("Teaching", 750),
        ("Children's Home", 750),
        ("Scholarships", 450),
        ("Outreach", 150),
    ]
    assert sum(report_lines.owed_back(i) for i in items) == 0  # the ministry's own cash


def test_their_own_cash_split_is_owed_back_once():
    from core.agents.expenses import report_lines

    items = _five_ways(ministry=False)
    assert sum(i["amount_cents"] for i in items) == 3000
    assert sum(report_lines.owed_back(i) for i in items) == 3000


def test_each_part_says_its_share_of_the_rupees_and_is_its_own_ledger_line():
    items = _five_ways(ministry=True)
    assert [i["said_amount"] for i in items] == [
        "1200.00",
        "1000.00",
        "1000.00",
        "600.00",
        "200.00",
    ]
    assert {i["said_currency"] for i in items} == {"NPR"}
    assert [i["part"] for i in items] == [1, 2, 3, 4, 5]  # one expense_line each, never one for all


def test_a_line_nobody_split_is_priced_as_it_always_was():
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR4000.00", "ministry", "the region")]
    (item,) = _card_items(log)
    assert item["amount_cents"] == 3000 and item["said_amount"] == "4000.00"
    assert not item.get("part")
