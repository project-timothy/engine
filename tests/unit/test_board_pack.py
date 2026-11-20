"""A ministry's statements from its own books (board chunk c29; the owner,
2026-11-12: the board gets its statements every month without anyone
building them by hand).

From the double-entry journal, by code: the statement of activities, the
statement of financial position, and the program and management split by
the share the ministry set. Net assets are the opening plus all that came
in less all that was spent, and every figure matches the books answer."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from core.agents.expenses import board_pack as bp
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_statements import MINISTRY_CSV

AT_END = {
    "Assets:Bank:Ruth": 120000,
    "Assets:Cash:Ruth": 5000,
    "Liabilities:People:Ruth": 3000,  # ministry money she holds
    "Liabilities:People:Ann": -2000,  # owed to her
    "Equity:Opening": -100000,
    "Income:Gifts": -50000,
    "Expenses:Meals": 10000,
    "Expenses:Scholarships": 14000,
}


def test_activities_read_income_and_spending_by_kind():
    acts = bp.activities(AT_END)
    assert acts.income == {"Gifts": 50000}
    assert acts.expenses == {"Meals": 10000, "Scholarships": 14000}
    assert (acts.total_income, acts.total_expenses, acts.change) == (50000, 24000, 26000)


def test_net_assets_are_the_opening_plus_what_came_in_less_what_was_spent():
    pos = bp.position(AT_END)
    assert pos.in_the_bank == {"Ruth": 120000} and pos.cash_in_hand == {"Ruth": 5000}
    assert pos.held_by_people == {"Ruth": 3000} and pos.owed_to_people == {"Ann": 2000}
    assert (pos.total_assets, pos.total_liabilities) == (128000, 2000)
    assert pos.net_assets == 126000 == 100000 + 50000 - 24000
    assert pos.ties


def test_an_account_the_statements_dont_know_never_ties_quietly():
    assert not bp.position(AT_END | {"Assets:Mystery": 100}).ties


def test_the_split_divides_each_kind_to_the_cent():
    f = bp.functional({"Meals": 10001, "Pay": 333}, 95)
    assert f.by_kind == {"Meals": (9501, 500), "Pay": (316, 17)}
    assert f.program + f.management == 10334


def test_the_newest_share_said_stands():
    assert bp.latest_share([]) is None
    assert bp.latest_share([{"percent": 90}, {"percent": 95}]) == 95


# ---- through the door ------------------------------------------------------------


def test_the_board_pack_is_the_books_own_and_balances(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    pack = door.call("board_pack", {"period": "2026-Q3"})
    books = door.call("books", {"period": "2026-Q3"})
    assert pack["matches_the_books"] and pack["balanced"] and pack["ties_to_the_bank"]
    assert pack["activities"]["came_in"] == books["came_in"]
    assert pack["activities"]["spent_by_kind"] == books["spent_by_kind"]
    pos = pack["position"]
    assert pos["in_the_bank"] == {"Daniel Reyes": "3374.74"}
    assert Decimal(pos["net_assets"]) == Decimal(pos["total_assets"]) - Decimal(
        pos["total_liabilities"]
    )
    assert "net assets were" in pack["says"]
    assert "set_program_share" in pack["functional"]["say"]  # not set yet


def test_the_split_once_set_reads_in_the_pack(solo):  # noqa: F811
    _keep_ministry(solo, MINISTRY_CSV)
    linda = _door(solo, "linda-park")
    said = linda.call("set_program_share", {"percent": "95", "said": "95 percent is program"})
    assert said["status"] == "set", said
    f = linda.call("board_pack", {"period": "2026-Q3"})["functional"]
    assert f["program_share"] == 95
    spent = linda.call("books", {"period": "2026-Q3"})["spent"]
    assert Decimal(f["program"]) + Decimal(f["management"]) == Decimal(spent)


def test_a_share_that_isnt_a_percent_is_asked_again(solo):  # noqa: F811
    linda = _door(solo, "linda-park")
    for bad in ("", "abc", "120"):
        assert linda.call("set_program_share", {"percent": bad, "said": bad})["status"] == "needs"


# ---- the page and the PDF ----------------------------------------------------------


def _pack(solo):  # noqa: F811
    _keep_ministry(solo, MINISTRY_CSV)
    linda = _door(solo, "linda-park")
    linda.call("set_program_share", {"percent": "95", "said": "95 percent"})
    return linda.call("board_pack", {"period": "2026-Q3"})


def test_the_pdf_reads_back_with_the_same_figures(solo):  # noqa: F811
    import io

    from pypdf import PdfReader

    from core.tools.board_pack_page import pdf

    pack = _pack(solo)
    reader = PdfReader(io.BytesIO(pdf(pack, "Living Water Mozambique, Inc.")))
    words = " ".join(page.extract_text() for page in reader.pages)
    assert "Statement of activities" in words and "Net assets" in words
    assert f"${Decimal(pack['position']['net_assets']):,.2f}" in words
    assert "95% program" in words and "Matches the books to the cent." in words


def test_the_page_escapes_what_it_shows_and_carries_no_script(solo):  # noqa: F811
    from core.tools.board_pack_page import html

    page = html(_pack(solo), "Grace & <Truth>")
    assert "Grace &amp; &lt;Truth&gt;" in page and "<script" not in page
    assert "$3,374.74" in page


def test_a_long_label_never_runs_into_its_dollars():
    from core.tools.paper import _M, _RIGHT, _width, _wrap

    label = "Ministry money held by " + "a very long name " * 6
    room = _RIGHT - _M - _width("$1,234,567.89", 10) - 12
    assert all(_width(part, 10) <= room for part in _wrap(label, 10, room))


def test_the_command_writes_the_page_and_the_pdf(solo, tmp_path, monkeypatch, capsys):  # noqa: F811
    from core.engine.cli import main

    _keep_ministry(solo, MINISTRY_CSV)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(solo["root"]))
    code = main(
        [
            "board-pack",
            "living-water",
            "--as",
            "linda-park",
            "--period",
            "2026-Q3",
            "--out",
            str(tmp_path / "pack"),
            "--ledger-dir",
            str(Path(solo["ledger_root"]).parent),
        ]
    )
    assert code == 0, capsys.readouterr()
    assert sorted(p.suffix for p in (tmp_path / "pack").iterdir()) == [".html", ".pdf"]


def test_money_is_measured_in_helveticas_own_widths():
    from core.tools.paper import _width

    # "$8,580.03": $ and six digits at 556, a comma and a period at 278.
    assert _width("$8,580.03", 10) == (7 * 556 + 2 * 278) / 100
