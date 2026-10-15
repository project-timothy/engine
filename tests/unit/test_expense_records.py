"""Records (the structure review's proposal 1, decided 2026-10-14).

Ruth's October was approved on Oct 13 with two receipts. On Oct 14 three
more landed, and her reviewer was asked about all five again (practice box
card #4). A yes is to the receipts a card named, so approved stays
approved: the late receipts wait on a small card of their own, which says
what is already approved. A receipt whose facts change after its yes goes
back in front of the reviewer, and the month builds once every receipt in
it carries a yes."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from core.agents.expenses import records
from core.engine.runner import run
from core.tools import decide
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_missionary_month import (
    NPR_OCT_12,
    _cards,
    _month,
    _reviewer,
    _rupees,
    feed,  # noqa: F401  (fixture)
)

NPR_OCT_13 = "152.44534601"


def _db(solo) -> Path:  # noqa: F811
    return Path(solo["ledger_root"]) / "ledger.sqlite3"


def _approve(solo, card: dict, at: str = "2026-10-13T16:40:00+00:00") -> None:  # noqa: F811
    con = sqlite3.connect(_db(solo))
    con.execute(
        "UPDATE approval_queue SET status = 'approved', resolved_at = ? WHERE id = ?",
        (at, card["id"]),
    )
    con.commit()
    con.close()


def _lines(solo) -> list[sqlite3.Row]:  # noqa: F811
    con = sqlite3.connect(_db(solo))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM expense_line ORDER BY id").fetchall()
    con.close()
    return rows


def _categorize(solo, sha: str, category: str):  # noqa: F811
    return run(
        "living-water",
        "expenses",
        "categorize",
        params={"sha": sha, "category": category, "by": "linda-park"},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _two_days(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    feed("2026-10-13", npr=NPR_OCT_13)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))  # $28.06
    _month(solo)
    [first] = _cards(solo)
    _approve(solo, first)
    _rupees(solo, datetime(2026, 10, 13, 9, 0, tzinfo=UTC), said="1000", photo=JPEG + b"2")
    _month(solo)
    return first


def test_a_late_receipt_waits_on_its_own_card_and_the_yes_stands(solo, feed):  # noqa: F811
    first = _two_days(solo, feed)
    cards = _cards(solo)
    assert [c["status"] for c in cards] == ["approved", "pending"]
    late = cards[-1]
    assert late["total_cents"] == "656"  # 1,000 / 152.4453 = $6.56, the late one alone
    assert "NPR 1,000.00" in late["lines"] and "4,300" not in late["lines"]
    assert late["already"] == "Already approved Oct 13: 1 receipt, $28.06"
    assert len(late["receipts"].split(",")) == 1
    assert cards[0]["id"] == first["id"]


def test_the_month_builds_once_every_receipt_has_its_yes(solo, feed):  # noqa: F811
    _two_days(solo, feed)
    _approve(solo, _cards(solo)[-1], at="2026-10-14T09:00:00+00:00")
    _month(solo)
    assert sorted(line["amount_cents"] for line in _lines(solo)) == [656, 2806]


def test_a_change_after_the_yes_asks_again(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    [first] = _cards(solo)
    _approve(solo, first)
    sha = first["receipts"]
    _categorize(solo, sha, "Meals")
    _month(solo)
    assert _lines(solo) == []  # never built on a value nobody approved
    cards = _cards(solo)
    assert [c["status"] for c in cards] == ["approved", "pending"]
    assert "Meals" in cards[-1]["lines"] and cards[-1]["receipts"] == sha


def test_the_card_reads_what_is_already_approved(solo, feed):  # noqa: F811
    _two_days(solo, feed)
    late = _cards(solo)[-1]
    row = {
        "agent": "expenses",
        "action_type": records.REVIEW_CARD,
        "params": {k: v for k, v in late.items() if k not in ("id", "status")},
    }
    words = decide.plain_words(row, "approve")
    assert words.splitlines()[-1] == "Already approved Oct 13: 1 receipt, $28.06."


def test_where_each_receipt_stands(solo, feed):  # noqa: F811
    _two_days(solo, feed)
    [month] = _reviewer(solo).call("month_report", {"month": "2026-10"})["people"]
    stands = sorted(line["stands"] for line in month["lines"])
    assert stands == ["approved", "said"]
    assert month["review"] == "Waiting for approval"


def test_the_life_of_a_record():
    sha = "a" * 64
    landed = {
        "event_type": records.LANDED_EVENT,
        "created_at": "2026-10-12T09:00:00+00:00",
        "payload": {"sha256": sha, "person": "Ruth", "month": "2026-10", "file": "x.jpg"},
    }
    placed = {
        "event_type": records.CATEGORY_EVENT,
        "created_at": "2026-10-12T10:00:00+00:00",
        "payload": {"sha256": sha, "category": "Meals"},
    }
    card = {
        "id": 3,
        "action_type": records.REVIEW_CARD,
        "status": "approved",
        "resolved_at": "2026-10-13T16:40:00+00:00",
        "params": {"receipts": sha},
    }

    def stage(events, cards=(), lines=()):
        return records.where(events, cards, lines)[sha].stage

    assert stage([landed]) == "captured"
    assert stage([landed, placed]) == "placed"
    assert stage([landed, placed], [card]) == "approved"
    assert records.where([landed], [card], [])[sha].approved_on.startswith("2026-10-13")
    later = {**placed, "created_at": "2026-10-14T08:00:00+00:00"}
    assert stage([landed, later], [card]) == "placed"  # changed after the yes
    line = {"receipt_sha256": sha, "status": records.STATUS_OPEN}
    assert stage([landed], [card], [line]) == "reported"
    paid = {"receipt_sha256": sha, "status": records.STATUS_REIMBURSED}
    assert stage([landed], [card], [paid]) == "paid back"


def test_a_card_from_before_records_still_builds(solo, feed):  # noqa: F811
    """An approved card raised before cards named their receipts (a host's
    report approved the day this ships) builds on its content, as before."""
    feed("2026-10-12", npr=NPR_OCT_12)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    [card] = _cards(solo)
    con = sqlite3.connect(_db(solo))
    params = {k: v for k, v in card.items() if k not in ("id", "status", "receipts", "already")}
    con.execute(
        "UPDATE approval_queue SET status = 'approved', params_json = ? WHERE id = ?",
        (json.dumps(params), card["id"]),
    )
    con.commit()
    con.close()
    _month(solo)
    assert [line["amount_cents"] for line in _lines(solo)] == [2806]
