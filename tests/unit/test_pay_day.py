"""Pay day: the ministry pays its missionary what the board set, and the books
say so (c34 PR 2, the sender model, the owner's go of 2026-11-14).

Tim never sends money. On the pay day it reminds whoever pays the ministry's
bills; when the bank line comes in, the person says it was the missionary's
pay, and the books count it under Pay, the ministry's spending on that
person, never something owed back. A line only reads as pay where the board
set pay for that person in that month, and a line that differs from the
month's pay is kept as paid with the difference said.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.engine.runner import run
from core.policy.pay import write
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_statements import MINISTRY_CSV
from tests.unit.test_year_close import _db, _yes


def _set_pay(solo, yearly: str = "1036.80", day: int = 12, start: str = "2026-08"):  # noqa: F811
    """The board's resolution for Daniel, adopted (his reviewer said yes)."""
    from decimal import Decimal

    written = write(
        "Living Water Mozambique, Inc.",
        person="daniel-reyes",
        name="Daniel Reyes",
        yearly=Decimal(yearly),
        day=day,
        start=start,
        adopted_by="reviewer",
    )
    folder = Path(solo["root"]) / "living-water" / "rules"
    (folder / written.copy).write_text(written.words, encoding="utf-8")
    (folder / written.file).write_text(written.rules, encoding="utf-8")
    run("living-water", "policy", "propose", tenants_root=solo["root"])
    con = sqlite3.connect(_db(solo))
    rows = con.execute(
        "SELECT id, params_json FROM approval_queue WHERE action_type = 'policy.plan'"
    ).fetchall()
    con.close()
    for card_id, params in rows:
        if json.loads(params).get("kin") == "yes":
            _yes(solo, card_id, at="2026-08-01T15:00:00+00:00")


def _line(door, day: str) -> str:
    return next(
        b["source"]
        for p in door.call("month_report", {"month": day[:7]})["people"]
        for b in p["bank"]
        if b["date"] == day
    )


def _row(door, day: str) -> dict:
    return next(
        b
        for p in door.call("month_report", {"month": day[:7]})["people"]
        for b in p["bank"]
        if b["date"] == day
    )


def test_the_line_that_paid_him_is_his_pay_in_the_books(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    done = daniel.call(
        "explain_bank_line",
        {
            "line": _line(daniel, "2026-08-12"),
            "said": "Daniel's August pay",
            "pay_of": "Daniel Reyes",
        },
    )
    assert done["status"] == "set", done
    assert done["say"] == "Done: the Aug 12, 2026 line was Daniel Reyes's pay for August, $86.40."
    row = _row(daniel, "2026-08-12")
    assert row["role"] == "their pay" and row["paid_to"] == "daniel-reyes"
    books = daniel.call("books", {"period": "August 2026"})
    de = books["double_entry"]
    assert de["balanced"] and de["ties_to_the_bank"]
    assert de["accounts_at_end"]["Expenses:Pay"] == "86.40"
    assert books["owed_back_to_people"] == "0.00"


def test_a_line_unlike_the_months_pay_is_kept_and_the_difference_said(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo, yearly="1200.00")
    done = daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "Daniel's pay", "pay_of": "Daniel Reyes"},
    )
    assert done["status"] == "set", done
    assert "$86.40" in done["say"] and "The board set $100.00 for August" in done["say"]


def test_no_resolution_for_that_month_means_it_is_not_pay(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo, start="2026-09")
    done = daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-12"), "said": "his pay", "pay_of": "Daniel Reyes"},
    )
    assert done["status"] == "asks"
    assert "no pay for Daniel Reyes in August 2026" in done["say"]
    assert _row(daniel, "2026-08-12")["role"] != "their pay"


def test_only_money_out_of_the_ministrys_account_is_pay(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _set_pay(solo)
    done = daniel.call(
        "explain_bank_line",
        {"line": _line(daniel, "2026-08-03"), "said": "pay", "pay_of": "Daniel Reyes"},
    )
    assert done["status"] == "asks"
    assert "Only a payment out of the ministry's own account" in done["say"]


# ---- the reminder -----------------------------------------------------------------


def _notices(solo, today: str) -> list[dict]:  # noqa: F811
    run(
        "living-water",
        "routing",
        "notices",
        params={"today": today},
        tenants_root=solo["root"],
    )
    con = sqlite3.connect(_db(solo))
    rows = con.execute(
        "SELECT payload_json FROM events WHERE event_type = 'route.notice'"
    ).fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows if json.loads(r[0]).get("kind") == "pay_due"]


def test_on_pay_day_whoever_pays_the_bills_hears_it_once(solo):  # noqa: F811
    _set_pay(solo)
    assert _notices(solo, "2026-08-11") == []
    [notice] = _notices(solo, "2026-08-12")
    assert notice["about"] == "daniel-reyes" and notice["month"] == "2026-08"
    assert notice["day"] == "2026-08-12"
    assert len(_notices(solo, "2026-08-13")) == 1  # once a month
    assert len(_notices(solo, "2026-09-12")) == 2  # and again next month


def test_no_resolution_no_reminder(solo):  # noqa: F811
    assert _notices(solo, "2026-08-12") == []
