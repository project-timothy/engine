"""The 990-N each May (chunk 5, board t-990n).

A small charity's yearly IRS filing is the 990-N e-Postcard, due the 15th
day of the fifth month after its year ends (setup already puts the Form 990
on its calendar, test_setup_deadlines). In April Tim's morning email tells
whoever runs the ministry's money that it's due, never a figure; asked,
Tim answers with the year's gross receipts from the books and the short
list of facts the form asks for.

Made-up numbers: Living Water's account took in $1,200.00 on Jan 15, $800.00
on Jun 30 and $0.25 of interest on Dec 31, 2026 ($2,000.25); $500.00 on
Jan 5, 2027 is next year's. Daniel's own account took in his pay, which
is never the ministry's.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from core.agents.deadlines import form990
from core.agents.expenses import statements as st
from core.agents.routing.jobs import NOTICE_EVENT
from core.engine.runner import run
from core.ledger import Ledger
from core.tools import catalog
from tests.unit.test_bank_catch_up import _daniel, _held, _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

GIFTS = """Date,Description,Debit,Credit
01/15/2026,DEPOSIT GIFT,,1200.00
06/30/2026,ZELLE FROM A SUPPORTER,,800.00
12/31/2026,INTEREST PAID,,0.25
01/05/2027,DEPOSIT GIFT,,500.00
"""


def _line(day: str, cents: int, account: str = st.MINISTRY) -> dict:
    payload = st.Line(day, f"DEPOSIT {day}", cents).payload("Daniel Reyes", account)
    return {"event_type": st.LINE_EVENT, "payload": payload}


def test_gross_receipts_are_what_came_into_the_ministrys_account_in_the_year():
    log = [
        _line("2026-01-15", 120000),
        _line("2026-06-30", 80000),
        _line("2026-07-01", -5000),  # money out is never a receipt
        _line("2026-03-01", 990000, st.OWN),  # the person's own pay
        _line("2027-01-05", 50000),
    ]
    assert st.came_in(log, "2026-01-01", "2026-12-31") == 200000


def test_a_transfer_back_in_is_no_receipt():
    back = _line("2026-02-01", 30000)
    moved = {"event_type": st.TRANSFER_EVENT, "payload": {"sha256": back["payload"]["sha256"]}}
    assert st.came_in([back, moved], "2026-01-01", "2026-12-31") == 0


def _tools(solo, today: str) -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        obligations_file=Path(solo["root"]) / "living-water" / "obligations.toml",
        today=today,
    )


def _with_gifts(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, GIFTS.encode())
    [held] = _held(door).values()
    door.call("bank_statement", {"statement": held["statement"], "account": "ministry"})


def test_asked_whats_due_tim_has_the_years_gross_receipts_and_what_the_form_asks(solo):  # noqa: F811
    _with_gifts(solo)
    rows = _tools(solo, "2027-04-16").call("deadlines", {"days": 60})["rows"]
    [row] = [r for r in rows if r["id"] == "form-990"]
    assert row["due"] == "2027-05-15"
    assert row["tax_year"] == "2026-01-01 to 2026-12-31"
    assert row["gross_receipts"] == "2000.25"
    assert "990-N" in row["files"] and "$50,000" in row["files"]
    assert "Employer identification number (EIN)" in row["asks_for"]
    assert row["known"] == {
        "legal name": "Living Water Mozambique, Inc.",
        "tax year": "2026-01-01 to 2026-12-31",
    }


def test_above_50000_the_990n_is_not_enough():
    assert "990-EZ" in form990.files(5_000_001) and "990-N" not in form990.files(5_000_001)
    assert "990-N" in form990.files(5_000_000)


def test_the_tax_year_is_the_one_that_ended_before_the_due_date():
    assert form990.tax_year_of(date(2027, 5, 15)) == ("2026-01-01", "2026-12-31")
    assert form990.tax_year_of(date(2026, 11, 15)) == ("2025-07-01", "2026-06-30")


def _notices(solo, today: str) -> list[dict]:  # noqa: F811
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
            if e["event_type"] == NOTICE_EVENT and e["payload"]["kind"] == "filing_990"
        ]


def test_in_april_whoever_runs_the_money_hears_once_that_it_is_due(solo):  # noqa: F811
    assert _notices(solo, "2027-03-20") == []  # 56 days out: not yet
    [notice] = _notices(solo, "2027-04-15")
    assert notice["to"] == "daniel-reyes"  # who runs the money; never the reviewer
    assert (notice["about"], notice["month"]) == ("", "2027-05")
    assert notice["id"] == "filing:form-990:2027-05-15:daniel-reyes"
    assert len(_notices(solo, "2027-04-16")) == 1  # once a year
