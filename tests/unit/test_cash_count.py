"""Caleb's cash, finished (chunk 1, 2026-10-15): the envelope is counted,
cash gone with no receipt is said for, a receipt that was partly the
person's own is split, and cash nobody accounts for grows old where the
person and the reviewer both see it.

Made-up numbers, as in test_atm_cash: $300 gave NPR 40,000 on Aug 3 and
$150 gave NPR 21,000 on Aug 20; a NPR 4,300 receipt on Aug 7. The books
hold NPR 56,700 by October. Daniel counts NPR 50,000: NPR 2,000 went to his
family and NPR 4,700 to tea, taxis and tips.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from core.agents.expenses import statements as st
from core.agents.expenses.records import CASH_COUNT_EVENT, CASH_USED_EVENT, RECEIPT_SPLIT_EVENT
from core.agents.routing.jobs import NOTICE_EVENT
from core.engine.runner import run
from core.ledger import Ledger
from tests.unit.test_atm_cash import ATM_CSV, PERSON, _gave, _receipt, _receipt_on
from tests.unit.test_bank_catch_up import _daniel, _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_missionary_month import _cards, _month, feed  # noqa: F401


def _used(sha: str, day: str, tag: str, whose: str, said: str = "") -> dict:
    payload = {"sha256": sha, "person": PERSON, "day": day, "tag": tag, "whose": whose}
    return {"event_type": CASH_USED_EVENT, "payload": payload | {"said": said}}


def _split(sha: str, own: str) -> dict:
    return {"event_type": RECEIPT_SPLIT_EVENT, "payload": {"sha256": sha, "own": own}}


def _drawn(ministry: bool = True) -> list[dict]:
    account = st.MINISTRY if ministry else st.OWN
    atm = {
        "event_type": st.LINE_EVENT,
        "payload": st.Line("2026-08-03", "ATM WITHDRAWAL 000123", -30000).payload(PERSON, account),
    }
    return [atm, _gave(atm, "NPR40000.00")]


# ---- by code -----------------------------------------------------------------------


def test_cash_spent_on_the_ministry_with_no_receipt_is_the_ministrys_spending():
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR3000.00", "ministry", "tea, taxis")]
    drawn, on_hand = st.cash(log)
    assert drawn["u1"].cents == 2250 and drawn["u1"].ministry
    assert on_hand[(PERSON, "NPR")][0] == Decimal("37000.00")
    [row] = [ln for ln in st.month_lines(log, "2026-08") if ln.get("cash_used")]
    assert (row["role"], row["cents"]) == (st.MINISTRY_PAID, 2250)
    assert row["words"] == "Cash with no receipt: tea, taxis"


def test_the_ministrys_cash_on_their_own_use_is_owed_to_the_ministry():
    log = [*_drawn(), _used("u1", "2026-08-10", "NPR2000.00", "own", "family")]
    [row] = st.month_lines(log, "2026-08")[-1:]
    assert (row["role"], row["cents"]) == (st.REPAY, 1500)
    assert st.REPAY not in st.SPENDING


def test_their_own_cash_on_their_own_use_is_nobodys_business():
    log = [*_drawn(ministry=False), _used("u1", "2026-08-10", "NPR2000.00", "own")]
    assert [ln for ln in st.month_lines(log, "2026-08") if ln["role"] == st.REPAY] == []


def test_a_split_receipt_paid_from_the_ministrys_cash_owes_back_their_part():
    sha = "r1" * 32
    log = [*_drawn(), _receipt(sha, "2026-08-07", "NPR3000.00"), _split(sha, "NPR1300.00")]
    drawn, on_hand = st.cash(log)
    assert drawn[sha].cents == 2250  # the ministry's NPR 3,000
    assert on_hand[(PERSON, "NPR")][0] == Decimal("35700.00")  # the whole NPR 4,300 left the cash
    [repay] = [ln for ln in st.month_lines(log, "2026-08") if ln["role"] == st.REPAY]
    assert repay["cents"] == 975 and repay["sha256"] == sha + st.OWN_PART


def test_a_split_receipt_still_finds_its_card_line_and_the_line_splits():
    sha = "r2" * 32
    line = {
        "event_type": st.LINE_EVENT,
        "payload": st.Line("2026-08-09", "BHATBHATENI", -4300).payload(PERSON, st.MINISTRY),
    }
    log = [line, _receipt(sha, "2026-08-08", "$30.00"), _split(sha, "$13.00")]
    rows = st.month_lines(log, "2026-08")
    paid = next(ln for ln in rows if ln["role"] == st.MINISTRY_PAID)
    repay = next(ln for ln in rows if ln["role"] == st.REPAY)
    assert paid["receipt"] == sha and (paid["cents"], repay["cents"]) == (3000, 1300)


def test_old_cash_ages_and_the_card_says_when_it_passes_120_days():
    log = _drawn()
    assert st.cash_age(log, "2026-09-30") == {(PERSON, "NPR"): ("2026-08-03", Decimal("40000"))}
    assert st.days_old("2026-08-03", "2026-09-30") == 58
    assert "past 120 days" not in st.cash_words_at(log, PERSON, "2026-11-30")
    assert "past 120 days unaccounted for" in st.cash_words_at(log, PERSON, "2026-12-31")


# ---- through the door ----------------------------------------------------------------


def _with_cash(solo):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, ATM_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    bank = door.call("month_report", {"month": "2026-08"})["people"][0]["bank"]
    atms = sorted((b for b in bank if b["role"] == "cash drawn"), key=lambda b: b["date"])
    door.call("explain_bank_line", {"line": atms[0]["source"], "said": "40,000 rupees"})
    door.call("explain_bank_line", {"line": atms[1]["source"], "said": "21,000 rupees"})
    _receipt_on(door, desk, token, solo, "2026-08-07", "4,300 rupees", JPEG + b"a")
    return door, desk, token


def test_a_count_finds_the_gap_and_their_words_close_it(solo, feed):  # noqa: F811
    door, _desk, _token = _with_cash(solo)
    asked = door.call("explain_cash", {"said": "tea", "whose": "ministry"})
    assert asked["status"] == "asks"  # count first

    gap = door.call("count_cash", {"said": "I have 50,000 rupees left"})
    assert (gap["status"], gap["gap"]) == ("gap", "NPR6700.00"), gap
    bare = door.call("explain_cash", {"said": "2,000 went to my family", "whose": "own"})
    assert bare["status"] == "asks", bare  # 2,000 what? Never the whole gap by mistake
    own = door.call("explain_cash", {"said": "2,000 rupees went to my family", "whose": "own"})
    assert own["status"] == "set" and "4,700.00 is still open" in own["say"], own
    tea = door.call("explain_cash", {"said": "tea, taxis and tips", "whose": "ministry"})
    assert "matches the books now" in tea["say"], tea
    assert door.call("count_cash", {"said": "50,000 rupees"})["status"] == "matches"

    [month] = door.call("month_report", {"month": "2026-10"})["people"]
    roles = {b["role"] for b in month["bank"]}
    assert {"the ministry paid", "owed to the ministry"} <= roles
    assert month["owed_to_ministry"] == "15.00"  # NPR 2,000 of the Aug 3 cash, at 133.33
    assert month["cash_counted"] == "NPR 50,000.00 on 2026-10-13"
    assert month["cash_oldest"].startswith("NPR 29,000.00 drawn 2026-08-03, 71 days ago")
    assert "account for it or return it" in month["cash_oldest"]

    _month(solo, "2026-10")
    door.call("send_month_for_review", {"month": "2026-10"})
    [card] = [c for c in _cards(solo, "2026-10") if "lines" in c]
    # The reviewer reads it (the dress rehearsal, 2026-10-15): whose part, owed which way.
    assert card["repay"].startswith("2026-")
    assert "$15.00 owed to the ministry" in card["repay"]
    assert "Daniel Reyes's own use of the ministry's cash" in card["repay"]
    assert "your" not in card["repay"].lower()
    assert card["cash"].startswith("NPR 50,000.00 in hand")
    assert card["total_cents"] == "0"  # the ministry paid for its own tea; nothing owed back


def test_a_cash_line_says_who_told_tim_and_when(solo, feed):  # noqa: F811
    """Persona night S05 (2026-11-16): a cash line said earlier the same day
    sat in the books, and when the person said they never logged it, their
    AI could not say who had or when. Each said line now carries both."""
    door, _desk, _token = _with_cash(solo)
    door.call("count_cash", {"said": "I have 50,000 rupees left"})
    door.call("explain_cash", {"said": "6,700 rupees went to tea and taxis", "whose": "ministry"})
    [month] = door.call("month_report", {"month": "2026-10"})["people"]
    [line] = [b for b in month["bank"] if b["words"].startswith("Cash with no receipt")]
    assert line["told_by"] == "Daniel Reyes", line
    assert line["told_at"][:4] == str(date.today().year), line
    assert "T" in line["told_at"], line  # a time, so it orders against today's talk


def test_more_cash_than_the_books_changes_nothing_until_they_say(solo, feed):  # noqa: F811
    door, _desk, _token = _with_cash(solo)
    more = door.call("count_cash", {"said": "60,000 rupees"})
    assert more["status"] == "more" and "3,300.00 more" in more["say"], more


def test_a_receipt_split_through_the_door(solo, feed):  # noqa: F811
    door, desk, token = _with_cash(solo)
    _receipt_on(door, desk, token, solo, "2026-08-25", "30,000 rupees", JPEG + b"b")
    month = door.call("month_report", {"month": "2026-08"})["people"][0]
    line = next(ln for ln in month["lines"] if ln["date"] == "2026-08-25")
    done = door.call(
        "split_line",
        {"line": line["source"], "said": "10,000 rupees was mine", "whose": "own"},
    )
    assert done["status"] == "set" and "NPR20000.00 of it was the ministry's" in done["say"]
    month = door.call("month_report", {"month": "2026-08"})["people"][0]
    line = next(ln for ln in month["lines"] if ln["date"] == "2026-08-25")
    assert line["dollars"] == "150.00"  # the ministry's NPR 20,000 of the Aug 3 cash
    assert month["owed_to_ministry"] == "75.00"  # their NPR 10,000, from the same cash


def test_the_morning_email_asks_for_a_count(solo, feed):  # noqa: F811
    _with_cash(solo)

    def notices(today: str) -> list[dict]:
        run(
            "living-water",
            "routing",
            "notices",
            params={"today": today},
            ledger_dir=Path(solo["ledger_root"]).parent,
            tenants_root=solo["root"],
        )
        with Ledger.open(solo["ledger_root"]) as ledger:
            return [
                e["payload"]
                for e in ledger.read_event_log()
                if e["event_type"] == NOTICE_EVENT and e["payload"]["kind"] == "cash_count"
            ]

    assert notices("2026-09-10") == []  # 38 days: not yet
    assert [n["id"] for n in notices("2026-09-17")] == ["cashage:2026-08-03:daniel-reyes"]
    assert len(notices("2026-09-18")) == 1  # once for that cash
    ids = [n["id"] for n in notices("2026-10-01")]
    assert "cash:2026-10:daniel-reyes" in ids
    assert all(n["about"] == "" and n["month"] == "" for n in notices("2026-10-01"))


def test_events_name_their_kinds():
    assert {CASH_COUNT_EVENT, CASH_USED_EVENT, RECEIPT_SPLIT_EVENT} == {
        "bank.cash_counted",
        "bank.cash_used",
        "expense.receipt_split",
    }


def test_an_adopted_plan_moves_the_return_day_and_says_cash_over_its_ceiling():
    from core.policy import Clock

    log = _drawn()
    plan = Clock(due=30, back=60, cash_up_to=Decimal("100"))
    said = st.cash_words_at(log, PERSON, "2026-10-15", plan)
    assert "past 60 days unaccounted for" in said
    assert "over the $100.00 the plan lets a person hold" in said
    assert "over the" not in st.cash_words_at(log, PERSON, "2026-10-15")


def test_the_same_words_twice_are_one_explanation(solo, feed):  # noqa: F811
    # Review 2026-11-10: an AI that sent the same words again (a retry) had the
    # cash recorded twice, 2,000 rupees becoming 4,000 owed.
    door, _desk, _token = _with_cash(solo)
    door.call("count_cash", {"said": "I have 50,000 rupees left"})
    words = {"said": "2,000 rupees went to my family", "whose": "own"}
    first = door.call("explain_cash", words)
    again = door.call("explain_cash", words)
    assert first["status"] == "set"
    assert again["status"] == "already", again
    assert "4,700.00 is still open" in again["say"]
    other = door.call("explain_cash", {"said": "another 700 rupees for my haircut", "whose": "own"})
    assert other["status"] == "set" and "4,000.00 is still open" in other["say"], other
