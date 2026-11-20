"""The books a CPA can open without Tim (board chunk c30; the owner,
2026-11-12: nothing locked inside Tim).

A year's journal as plain text in the Beancount format, a trial balance as
CSV, and the numbers the 990 and the 1099s start from. Read back, the file
balances and says what the books say; a year opens from what was brought
forward, so it stands alone."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from core.agents.expenses import export
from core.agents.expenses import journal as j
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_statements import MINISTRY_CSV


def test_account_names_become_beancounts_and_keep_a_map_home():
    assert export.account_name("Expenses:Children's Home") == "Expenses:Childrens-Home"
    assert export.account_name("Assets:Bank:Caleb Meer") == "Assets:Bank:Caleb-Meer"
    assert export.account_name("Income:gifts (online)") == "Income:Gifts-online"
    text = export.beancount(
        [j.Entry("2026-01-02", "Gift", (("Assets:Bank:A B", 500), ("Income:Gifts", -500)), "x")],
        title="Test",
    )
    assert ";   Assets:Bank:A-B = Assets:Bank:A B" in text


def test_the_file_reads_back_to_the_same_totals_and_balances():
    entries = [
        j.Entry(
            "2026-01-02",
            'A "quoted" gift',
            (("Assets:Bank:A", 50000), ("Income:Gifts", -50000)),
            "b1",
        ),
        j.Entry("2026-02-03", "Rice", (("Expenses:Food", 1234), ("Assets:Bank:A", -1234)), "b2"),
    ]
    back = export.read_back(export.beancount(entries, title="T"))
    assert back.unbalanced == 0 and back.transactions == 2
    assert back.totals == {export.account_name(a): c for a, c in j.totals(entries).items()}


def test_an_unbalanced_transaction_reads_back_as_one():
    text = '2026-01-01 * "x"\n  Assets:Bank:A   1.00 USD\n  Income:Gifts   -0.99 USD\n'
    assert export.read_back(text).unbalanced == 1


def test_a_year_opens_from_what_was_brought_forward():
    before = [
        j.Entry(
            "2025-06-01",
            "Opening balance",
            (("Assets:Bank:A", 100000), ("Equity:Opening", -100000)),
            "o",
        ),
        j.Entry("2025-07-01", "Gift", (("Assets:Bank:A", 20000), ("Income:Gifts", -20000)), "g"),
        j.Entry(
            "2025-08-01", "Sent", (("Liabilities:People:A", 5000), ("Assets:Bank:A", -5000)), "s"
        ),
    ]
    carried = export.brought_forward(before, "2026-01-01")
    assert carried is not None and carried.balanced()
    assert dict(carried.legs) == {
        "Assets:Bank:A": 115000,
        "Liabilities:People:A": 5000,
        "Equity:Opening": -120000,  # the opening plus last year's 20,000 in
    }


def test_the_trial_balance_debits_equal_its_credits():
    rows = list(
        csv.reader(io.StringIO(export.trial_balance({"Assets:Bank:A": 500, "Income:Gifts": -500})))
    )
    assert rows[0] == ["Account", "Ministry's name", "Debit", "Credit"]
    assert rows[-1] == ["Total", "", "5.00", "5.00"]


def test_the_filings_start_from_gross_receipts_and_the_pay_kind():
    year = {"Income:Gifts": -1386798, "Expenses:Pay": 858003, "Expenses:Meals": 27570}
    at_end = {"Assets:Bank:C": 118230, "Liabilities:People:C": 232428}
    f = export.filings(year, at_end, "Pay")
    assert f["gross_receipts"] == "13867.98" and f["990"].startswith("990-N")
    assert f["spent_under_pay"] == "8580.03" and f["held_or_owed_by_people"] == {"C": "2324.28"}
    assert export.filings({"Income:Gifts": -5_000_001}, {}, "")["990"].startswith("990-EZ")


# ---- through the door --------------------------------------------------------------


def test_the_export_round_trips_and_matches_the_books(solo):  # noqa: F811
    _keep_ministry(solo, MINISTRY_CSV)
    linda = _door(solo, "linda-park")
    done = linda.call("cpa_export", {"year": "2026"})
    assert done["round_trips"] and done["balanced"] and done["matches_the_books"], done["says"]
    books = linda.call("books", {"period": "2026"})
    assert done["filings"]["gross_receipts"] == books["came_in"]
    assert "Assets:Bank:Daniel-Reyes" in done["beancount"]
    assert done["trial_balance_csv"].splitlines()[-1].startswith("Total,")


def test_the_command_writes_the_three_files(solo, tmp_path, monkeypatch, capsys):  # noqa: F811
    from core.engine.cli import main

    _keep_ministry(solo, MINISTRY_CSV)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(solo["root"]))
    out = tmp_path / "cpa"
    code = main(
        [
            "cpa-export",
            "living-water",
            "--as",
            "linda-park",
            "--year",
            "2026",
            "--out",
            str(out),
            "--ledger-dir",
            str(Path(solo["ledger_root"]).parent),
        ]
    )
    assert code == 0, capsys.readouterr()
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        "living-water-books-2026-filings.json",
        "living-water-books-2026-trial-balance.csv",
        "living-water-books-2026.beancount",
    ]
    assert json.loads((out / names[0]).read_text())["gross_receipts"]
