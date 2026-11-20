"""The board pack as a page and a PDF (board chunk c29): the statements a
board reads, for people who don't use an AI. Built from ``board_pack``'s own
answer, so the page, the PDF and Tim say the same figures. The layout is
``paper``'s, shared with the donor letters (c32).
"""

from __future__ import annotations

from decimal import Decimal

from . import paper


def _usd(value: str) -> str:
    amount = Decimal(value)
    return f"-${-amount:,.2f}" if amount < 0 else f"${amount:,.2f}"


def sections(pack: dict) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    """The pack as titled tables of (label, dollars, is_total) rows: the one
    shape both the page and the PDF lay out."""
    a, p = pack["activities"], pack["position"]
    out: list[tuple[str, list[tuple[str, str, bool]]]] = []
    rows = [(k, _usd(v), False) for k, v in a["came_in_by_kind"].items()]
    rows.append(("Total came in", _usd(a["came_in"]), True))
    rows += [(k, _usd(v), False) for k, v in a["spent_by_kind"].items()]
    rows.append(("Total spent", _usd(a["spent"]), True))
    rows.append(("Change in net assets", _usd(a["change_in_net_assets"]), True))
    out.append(("Statement of activities", rows))
    rows = [(f"In the bank ({k})", _usd(v), False) for k, v in p["in_the_bank"].items()]
    rows += [(f"Cash in hand ({k})", _usd(v), False) for k, v in p["cash_in_hand"].items()]
    rows += [(f"With {k}", _usd(v), False) for k, v in p.get("with_payment_services", {}).items()]
    rows += [
        (f"Ministry money held by {k}", _usd(v), False) for k, v in p["held_by_people"].items()
    ]
    rows.append(("Total assets", _usd(p["total_assets"]), True))
    rows += [(f"Owed to {k}", _usd(v), False) for k, v in p["owed_to_people"].items()]
    rows.append(("Total owed", _usd(p["total_liabilities"]), True))
    rows.append(("Net assets", _usd(p["net_assets"]), True))
    out.append((f"Statement of financial position, {p['as_of']}", rows))
    f = pack.get("functional", {})
    if "program_share" in f:
        rows = [
            (f"{k}: program / management", f"{_usd(v['program'])} / {_usd(v['management'])}", False)
            for k, v in f["by_kind"].items()
        ]
        rows.append(("Program", _usd(f["program"]), True))
        rows.append(("Management", _usd(f["management"]), True))
        out.append((f"Spending by function ({f['program_share']}% program)", rows))
    rows = [
        (m["month"], f"in {_usd(m['came_in'])}, spent {_usd(m['spent'])}", False)
        for m in pack["months"]
    ]
    out.append(("Each month", rows))
    return out


def checks(pack: dict) -> list[str]:
    """What the pack proves about itself, in words."""
    said = [
        "Matches the books to the cent."
        if pack["matches_the_books"]
        else "Does NOT match the books.",
        "Balances in double entry." if pack["balanced"] else "Does NOT balance.",
    ]
    if "ties_to_the_bank" in pack:
        said.append(
            "Agrees with the bank's own balance."
            if pack["ties_to_the_bank"]
            else "Does NOT agree with the bank's balance."
        )
    return said


def blocks(pack: dict, ministry: str) -> list[paper.Block]:
    """The pack as ``paper`` blocks: its title, what it says, each statement, its checks."""
    out: list[paper.Block] = [("title", f"{ministry}: {pack['period']}"), ("text", pack["says"])]
    out += [("table", title, rows) for title, rows in sections(pack)]
    out.append(("small", " ".join(checks(pack))))
    return out


def html(pack: dict, ministry: str) -> str:
    return paper.html(f"{ministry} board pack", blocks(pack, ministry))


def pdf(pack: dict, ministry: str) -> bytes:
    """The pack as a PDF, one or more Letter pages."""
    return paper.pdf(blocks(pack, ministry))
