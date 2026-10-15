"""The missionary's month (alpha step 3, the owner's decisions of 2026-10-13).

Ruth pays 4,300 rupees in Kathmandu. Her words are kept as she said them;
her reviewer sees the receipt in dollars at the rate on the receipt's own
date, from a free daily feed, with the rate and where it came from on the
line. The rate is recorded once and never moves. When the feed has no rate
for the day, the line waits and says so; nothing is guessed. The month job
raises one review card per person, and the reviewer asks for the month
through the door."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.agents.expenses import fx
from core.agents.expenses.jobs import REVIEW_CARD
from core.agents.expenses.schema import parse_foreign_claim
from core.engine.runner import run
from core.tools import catalog
from core.tools.receipts import Claim, ReceiptDesk, settled, take_photo
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG

NPR_OCT_12 = "153.25665518"  # the feed's own number for 2026-10-12


# ---- the rate ------------------------------------------------------------------------


def test_rupees_become_dollars_at_the_days_rate():
    rate = fx.Rate("NPR", "2026-10-12", NPR_OCT_12, fx.SOURCE)
    assert rate.cents(Decimal("4300")) == 2806  # 4,300 / 153.2567 = $28.06


def test_the_feed_gives_the_rate_for_that_day_exactly():
    asked = []

    def get(url):
        asked.append(url)
        return b'{"date": "2026-10-12", "usd": {"npr": 153.25665518, "mzn": 63.81}}'

    rate = fx.fetch("NPR", "2026-10-12", get=get)
    assert rate == fx.Rate("NPR", "2026-10-12", NPR_OCT_12, fx.SOURCE)
    assert "@2026-10-12/" in asked[0]  # the receipt's own date, never today's


def test_the_mirror_answers_when_the_feed_is_down():
    def get(url):
        if "jsdelivr" in url:
            raise OSError("down")
        return b'{"date": "2026-10-12", "usd": {"npr": 153.25665518}}'

    assert fx.fetch("NPR", "2026-10-12", get=get).per_usd == NPR_OCT_12


@pytest.mark.parametrize(
    "body",
    [
        b'{"date": "2026-10-11", "usd": {"npr": 153.1}}',  # another day's rate
        b'{"date": "2026-10-12", "usd": {"eur": 0.9}}',  # no rupee
        b'{"date": "2026-10-12", "usd": {"npr": 0}}',
        b"not json",
    ],
)
def test_no_rate_for_the_day_is_no_rate(body):
    assert fx.fetch("NPR", "2026-10-12", get=lambda url: body) is None


def test_the_first_rate_recorded_for_a_day_stands():
    first = fx.Rate("NPR", "2026-10-12", NPR_OCT_12, fx.SOURCE)
    later = fx.Rate("NPR", "2026-10-12", "150", fx.SOURCE)
    events = [{"event_type": fx.RATE_EVENT, "payload": r.payload()} for r in (first, later)]
    assert fx.stored(events)[("NPR", "2026-10-12")] == first


# ---- the claim -----------------------------------------------------------------------


def test_a_door_receipt_name_carries_the_foreign_claim():
    assert parse_foreign_claim("2026-10-12_receipt_NPR4300.00.jpg") == (
        "2026-10-12",
        Decimal("4300.00"),
        "NPR",
    )
    assert parse_foreign_claim("2026-10-12_receipt_NPR4300.00-2.jpg")[1] == Decimal("4300.00")
    assert parse_foreign_claim("2026-10-12_receipt_$18.75.jpg") is None
    assert parse_foreign_claim("2026-10-12_receipt_4300.00-rupees.jpg") is None
    assert parse_foreign_claim("Shoprite NPR4300.00.pdf") is None


@pytest.mark.parametrize(
    ("unit", "zone", "code"),
    [
        ("rupees", "Asia/Kathmandu", "NPR"),
        ("rupees", "Asia/Kolkata", "INR"),
        ("Rupee", "Asia/Karachi", "PKR"),
        ("meticais", "America/New_York", "MZN"),  # one country uses the word
        ("shillings", "Africa/Kampala", "UGX"),
        ("rupees", "America/New_York", ""),  # not settled: the card says so
    ],
)
def test_a_currency_word_is_settled_by_where_the_person_is(unit, zone, code):
    assert settled(Claim(Decimal("4300"), "", unit), zone).currency == code


def test_a_named_code_is_never_second_guessed():
    claim = Claim(Decimal("4300"), "INR", "INR")
    assert settled(claim, "Asia/Kathmandu") == claim


# ---- the month, end to end --------------------------------------------------------


@pytest.fixture
def feed(tmp_path, monkeypatch):
    """A file copy of the feed, one day per file, the way a proving-ground
    run points ENGINE_FX_FEED."""
    days = tmp_path / "feed"
    days.mkdir()
    monkeypatch.setenv("ENGINE_FX_FEED", f"file://{days}/{{day}}.json")

    def publish(day: str, **rates: str) -> None:
        (days / f"{day}.json").write_text(
            json.dumps({"date": day, "usd": {}})[:-2]
            + ", ".join(f'"{k}": {v}' for k, v in rates.items())
            + "}}"
        )

    return publish


def _rupees(solo, at: datetime, said: str = "4300", photo: bytes = JPEG) -> str:  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    desk = ReceiptDesk()
    token = desk.issue("daniel-reyes", "Daniel Reyes", Claim(Decimal(said), "", "rupees"))
    status, body = take_photo(
        desk,
        token,
        photo,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        now=at,
    )
    assert status == 200, body
    return body["say"]


def _month(solo, month: str = "2026-10"):  # noqa: F811
    return run(
        "living-water",
        "expenses",
        "month",
        params={"month": month},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _cards(solo, month: str = "2026-10") -> list[dict]:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ?", (REVIEW_CARD,)
    ).fetchall()
    con.close()
    out = [{"id": r["id"], "status": r["status"], **json.loads(r["params_json"])} for r in rows]
    return [c for c in out if c.get("month") == month]


def _reviewer(solo, person: str = "linda-park") -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        today="2026-10-13",
    )


def test_a_rupee_receipt_files_under_nepals_code(solo):  # noqa: F811
    said = _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    names = [p.name for p in Path.cwd().rglob("*_receipt_*")]
    assert any(n.startswith("2026-10-12_receipt_NPR4300.00") for n in names), names
    assert "4,300.00 rupees" in said and "monthly report" in said


def test_the_reviewer_sees_the_rupee_receipt_in_dollars_at_its_own_dates_rate(
    solo,  # noqa: F811
    feed,
):
    feed("2026-10-12", npr=NPR_OCT_12)
    feed("2026-10-13", npr="152.44534601")  # a later day's rate never applies
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    result = _month(solo)
    assert result.status in ("ok", "needs_approval"), result.summary

    [card] = _cards(solo)
    assert card["person"] == "Daniel Reyes" and card["total_cents"] == "2806"
    assert "NPR 4,300.00 at 153.26 per dollar on 2026-10-12" in card["lines"]

    [month] = _reviewer(solo).call("month_report", {"month": "2026-10"})["people"]
    assert month["total"] == "28.06" and month["review"] == "Waiting for approval"
    [line] = month["lines"]
    assert line["said"] == "NPR 4,300.00" and line["dollars"] == "28.06"
    assert line["rate"] == "NPR 153.26 to the dollar on 2026-10-12"
    assert line["date"] == "2026-10-12" and line["rate_from"]


def test_with_no_rate_for_the_day_the_line_waits_and_nothing_is_guessed(
    solo,  # noqa: F811
    feed,
):
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    assert _cards(solo) == []  # no card with a guessed number on it
    [month] = _reviewer(solo).call("month_report", {})["people"]
    assert month["waiting_on_a_rate"] == 1 and month["total"] == "0.00"
    assert month["lines"][0]["dollars"] is None
    assert month["lines"][0]["waiting_on"] == "the day's exchange rate"

    feed("2026-10-12", npr=NPR_OCT_12)  # published later
    _month(solo)  # same receipts, same day: a pending rate is asked again
    [card] = _cards(solo)
    assert card["total_cents"] == "2806"


def test_the_rate_is_recorded_once_and_a_new_receipt_retires_the_old_card(
    solo,  # noqa: F811
    feed,
):
    feed("2026-10-12", npr=NPR_OCT_12)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    feed("2026-10-12", npr="999")  # the feed changes its mind; the month does not
    _rupees(solo, datetime(2026, 10, 12, 10, 0, tzinfo=UTC), said="1000", photo=JPEG + b"2")
    _month(solo)
    cards = _cards(solo)
    assert [c["status"] for c in cards] == ["superseded", "pending"]
    assert cards[-1]["total_cents"] == str(2806 + 653)  # 1,000 / 153.2567 = $6.53


def test_the_approved_month_files_each_line_with_its_rate(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    [card] = _cards(solo)
    db = Path(solo["ledger_root"]) / "ledger.sqlite3"
    con = sqlite3.connect(db)
    con.execute("UPDATE approval_queue SET status = 'approved' WHERE id = ?", (card["id"],))
    con.commit()
    con.close()
    _month(solo)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    line = con.execute("SELECT * FROM expense_line").fetchone()
    con.close()
    assert line["amount_cents"] == 2806
    assert (line["said_amount"], line["said_currency"]) == ("4300.00", "NPR")
    assert (line["fx_rate"], line["fx_date"]) == (NPR_OCT_12, "2026-10-12")
    [month] = _reviewer(solo).call("month_report", {"month": "2026-10"})["people"]
    assert month["review"] == "Open" and month["lines"][0]["dollars"] == "28.06"


def test_the_month_job_reviews_the_month_that_just_ended_by_default(solo):  # noqa: F811
    result = _month(solo, month="")
    assert result.status == "ok"
    assert "no receipts" in result.summary


def test_only_the_reviewer_and_the_person_see_the_month(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    for who in ("linda-park", "daniel-reyes"):
        people = _reviewer(solo, who).call("month_report", {})["people"]
        assert [p["person"] for p in people] == ["Daniel Reyes"], who
