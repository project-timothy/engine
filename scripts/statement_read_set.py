"""Build the catch-up statement reader's eval set: made-up bank statements and
what each should read (Standalone Tim card R2, core/tools/statement_read.py).

    uv run python scripts/statement_read_set.py            # write the set
    uv run python scripts/statement_read_set.py --check    # rebuild in memory, compare (Mac)

Every statement below is invented: made-up banks, made-up accounts, made-up
amounts. Two banks' layouts (one signed Amount column with a summary box; a
credit union's Withdrawals, Deposits, and running Balance), a statement that
runs to two pages, a crooked photo, and one whose printed lines do not add
up to its printed balances. A case states the faithful reading; what code
does with it is code's business (core/tools/statement_read.py ``check``),
and a test holds that the one that doesn't add up never gets past it. The
drawing is ``scripts/picture_set.py``'s, shared with the receipt set; the
pictures are Mac-built and held by ``manifest.json`` (see there).
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from picture_set import _STREAM, MANIFEST, ROOT, document, drawn, picture  # noqa: E402, F401
from picture_set import main as write_or_check  # noqa: E402

from core.tools.statement_read import JOB, SYSTEM, USER  # noqa: E402

SET = ROOT / "core" / "llm" / "eval_sets" / JOB
WIDTH = 470  # points: a statement's columns, in a typewriter face
MINIMUM = 5
"""Cases the set must hold (tests/unit/test_llm_eval_sets.py)."""

Row = tuple[str, str, str]  # (ISO day, the bank's words, signed amount)


def _money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _lantern(
    period: tuple[str, str], opening: str, rows: list[Row], *, summary: bool, closing: str = ""
) -> tuple[list[str], dict]:
    """Lantern Prairie Bank: a summary box, then one signed Amount column.
    ``closing`` overrides the ending balance the statement prints."""
    start, end = period
    total_in = sum(Decimal(a) for _d, _w, a in rows if Decimal(a) > 0)
    total_out = -sum(Decimal(a) for _d, _w, a in rows if Decimal(a) < 0)
    ending = Decimal(closing) if closing else Decimal(opening) + total_in - total_out
    lines = [
        "LANTERN PRAIRIE BANK                              Member FDIC",
        "Hope Springs Ministries Inc.          Business Checking ...4417",
        f"Statement period {start[5:7]}/{start[8:]}/{start[:4]} - {end[5:7]}/{end[8:]}/{end[:4]}",
        f"Beginning balance                              ${_money(Decimal(opening))}",
    ]
    if summary:
        lines += [
            f"Deposits and credits                           ${_money(total_in)}",
            f"Withdrawals and debits                         ${_money(total_out)}",
        ]
    lines += [f"Ending balance                                 ${_money(ending)}", ""]
    lines.append("DATE   DESCRIPTION                                 AMOUNT")
    for day, words, amount in rows:
        if day == "\f":
            lines += ["(continued on next page)", "\f", "LANTERN PRAIRIE BANK   ...4417   page 2"]
            continue
        lines.append(f"{day[5:7]}/{day[8:]}  {words:<40}{_money(Decimal(amount)):>12}")
    expect = {
        "period_start": start,
        "period_end": end,
        "opening_balance": opening,
        "closing_balance": f"{ending:.2f}",
    }
    if summary:
        expect |= {"total_in": f"{total_in:.2f}", "total_out": f"{total_out:.2f}"}
    return lines, expect


def _tidewater(period: tuple[str, str], opening: str, rows: list[Row]) -> tuple[list[str], dict]:
    """Tidewater Hollow Credit Union: Withdrawals and Deposits apart, a running
    Balance, and days printed without their year."""
    start, end = period
    balance = Decimal(opening)
    first, last = (f"{d[5:7]}/{d[8:]}/{d[:4]}" for d in period)
    lines = [
        "TIDEWATER HOLLOW CREDIT UNION",
        "Share Draft Account ...0932   Hope Springs Ministries Inc.",
        f"Period {first} through {last}",
        f"Previous balance                                    {_money(balance)}",
        "",
        "DATE    DESCRIPTION             WITHDRAWALS   DEPOSITS     BALANCE",
    ]
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    for day, words, amount in rows:
        money = Decimal(amount)
        balance += money
        out, came = (_money(-money), "") if money < 0 else ("", _money(money))
        when = f"{months[int(day[5:7]) - 1]} {day[8:]}"
        lines.append(f"{when}  {words:<24}{out:>11}{came:>11}{_money(balance):>12}")
    lines.append(f"New balance                                         {_money(balance)}")
    expect = {
        "period_start": start,
        "period_end": end,
        "opening_balance": opening,
        "closing_balance": f"{balance:.2f}",
    }
    return lines, expect


OCTOBER: list[Row] = [
    ("2026-10-01", "ONLINE GIFT M HOLLIS", "150.00"),
    ("2026-10-03", "CHECK 1742", "-320.00"),
    ("2026-10-06", "PRAIRIE GAS #22 SALINA KS", "-48.17"),
    ("2026-10-09", "DEPOSIT CEDAR HOLLOW CHURCH", "1800.00"),
    ("2026-10-14", "ONLINE TRANSFER TO SAVINGS", "-600.00"),
    ("2026-10-17", "PAPER & INK SUPPLY", "-73.40"),
    ("2026-10-22", "ONLINE GIFT J ARNOLD", "200.00"),
    ("2026-10-28", "MONTHLY SERVICE FEE", "-12.00"),
]
SEPTEMBER: list[Row] = [
    ("2026-09-02", "ACH DEP MILLBROOK CHAPEL", "500.00"),
    ("2026-09-04", "CORNER FRESH MARKET", "-64.38"),
    ("2026-09-11", "ATM WITHDRAWAL 2140 MAIN", "-100.00"),
    ("2026-09-15", "BLUEWAVE MOBILE", "-85.12"),
    ("2026-09-19", "MOBILE DEPOSIT", "75.00"),
    ("2026-09-25", "RIVERSIDE BOOKS", "-29.99"),
    ("2026-09-30", "DIVIDEND", "0.41"),
]
AUGUST: list[Row] = [
    ("2026-08-01", "ONLINE GIFT HOLLIS FAMILY", "250.00"),
    ("2026-08-03", "CHECK 1731", "-450.00"),
    ("2026-08-05", "INTL WIRE TO NEPAL", "-500.00"),
    ("2026-08-07", "CORNER FUEL DAYTON OH", "-41.26"),
    ("2026-08-10", "DEPOSIT STONEBRIDGE CHURCH", "1200.00"),
    ("2026-08-12", "PAPER & INK SUPPLY", "-36.88"),
    ("2026-08-15", "ONLINE GIFT T NGUYEN", "100.00"),
    ("\f", "", "0"),
    ("2026-08-18", "CHECK 1732", "-275.00"),
    ("2026-08-20", "PEACEFUL HILL LODGE", "-1204.50"),
    ("2026-08-24", "ONLINE GIFT HOLLIS FAMILY", "250.00"),
    ("2026-08-27", "POST OFFICE 3812", "-14.85"),
    ("2026-08-31", "MONTHLY SERVICE FEE", "-12.00"),
]
JULY: list[Row] = [
    ("2026-07-01", "DEPOSIT WILLOW RUN", "650.00"),
    ("2026-07-08", "MAMA GRACE GROCERS", "-42.70"),
    ("2026-07-12", "WIRE FEE", "-25.00"),
    ("2026-07-20", "WIRE TO NAKURU PARTNER", "-1000.00"),
    ("2026-07-29", "ONLINE GIFT P OKAFOR", "120.00"),
]
JUNE: list[Row] = [
    ("2026-06-02", "ONLINE GIFT R DAVIS", "100.00"),
    ("2026-06-09", "CHECK 1019", "-250.00"),
    ("2026-06-16", "CORNER FRESH MARKET", "-63.20"),
    ("2026-06-23", "DEPOSIT FOXGLOVE CHAPEL", "400.00"),
    ("2026-06-30", "SERVICE FEE", "-12.00"),
]


def statements() -> list[tuple[str, str, list[str], dict, list[Row], str]]:
    """(name, look, lines, expect, rows, note) for every statement."""
    honest = "the lines, balances, and period read off the picture"
    return [
        (
            "lantern_october",
            "plain",
            *_lantern(("2026-10-01", "2026-10-31"), "2418.36", OCTOBER, summary=True),
            OCTOBER,
            f"one bank's layout, a signed Amount column and its totals: {honest}",
        ),
        (
            "tidewater_september",
            "plain",
            *_tidewater(("2026-09-01", "2026-09-30"), "812.05", SEPTEMBER),
            SEPTEMBER,
            f"a credit union's layout, withdrawals and deposits apart, days with no year: {honest}",
        ),
        (
            "lantern_august_two_pages",
            "two_pages",
            *_lantern(("2026-08-01", "2026-08-31"), "5120.00", AUGUST, summary=True),
            AUGUST,
            f"a statement on two pages, its ending balance on the first: {honest}",
        ),
        (
            "tidewater_july_tilted",
            "tilted",
            *_tidewater(("2026-07-01", "2026-07-31"), "1950.00", JULY),
            JULY,
            f"a crooked photo: {honest}",
        ),
        (
            "lantern_june_does_not_add_up",
            "plain",
            # The lines come to 1,074.80; the statement prints 1,047.80.
            *_lantern(
                ("2026-06-01", "2026-06-30"), "900.00", JUNE, summary=False, closing="1047.80"
            ),
            JUNE,
            "printed lines that do not add up to the printed ending balance: read faithfully, "
            "and code turns it away (tests/unit/test_statement_read.py)",
        ),
    ]


def build() -> dict[str, bytes]:
    """Every file of the set, by its path under the set's directory."""
    files: dict[str, bytes] = {}
    job = {
        "job": JOB,
        "output_model": "core.tools.statement_read:StatementReply",
        "decimal_fields": ["closing_balance", "lines", "opening_balance", "total_in", "total_out"],
        "prompt_source": "core/tools/statement_read.py SYSTEM and USER, as the catch-up link "
        "sends them; documents and cases built by scripts/statement_read_set.py",
        "built_by": "scripts/statement_read_set.py",
        "system": SYSTEM,
        "user": USER,
    }
    files["job.json"] = (json.dumps(job, indent=2) + "\n").encode()
    for name, look, lines, expect, rows, note in statements():
        file, data = document(name, look, lines, width=WIDTH)
        files[f"documents/{file}"] = data
        case = {
            "name": name,
            "note": note,
            "document": file,
            "text": "\n".join(lines),
            "expect": {
                "doc_type": "bank_statement",
                "currency": "USD",
                **expect,
                "lines": [{"day": d, "amount": a} for d, _w, a in rows if d != "\f"],
            },
        }
        files[f"cases/{name}.json"] = (
            json.dumps(case, indent=2, ensure_ascii=False) + "\n"
        ).encode()
    return files


def main(argv: list[str]) -> int:
    return write_or_check(argv, SET, build(), f"{len(statements())} statements")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
