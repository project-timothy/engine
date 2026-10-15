"""Every page the engine serves a person, rendered to files for the phone
photos CI keeps (structure review proposal 4): the receipt camera page, the
catch-up page, and the month page (a full month with its Approve button, an
empty month, and the timed-out note). Made-up content only.

    uv run python tests/browser/pages.py <out dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.tools import month_page, receipts

MINISTRY = "Grace Fellowship"

RUTH = {
    "person": "Ruth Hollis",
    "total": "56.96",  # the bank line still waits, so it is not in the total
    "review": "Waiting for approval",
    "waiting_on_a_rate": 0,
    "bank_waiting": 1,
    "lines": [
        {
            "date": "2026-10-12",
            "said": "NPR 4,300.00",
            "dollars": "28.06",
            "rate": "NPR 153.26 to the dollar on 2026-10-12",
            "category": "Travel",
            "fund": None,
            "waiting_on": None,
        },
        {
            "date": "2026-10-14",
            "said": "$18.75",
            "dollars": "18.75",
            "rate": None,
            "category": "Meals",
            "fund": None,
            "waiting_on": None,
        },
    ],
    "drives": [
        {
            "date": "2026-10-09",
            "miles": "14.0",
            "for": "visiting the church in Pokhara",
            "ministry": True,
            "dollars": "10.15",
            "waiting_on": None,
        }
    ],
    "bank": [
        {
            "date": "2026-10-03",
            "words": "PATHAO KATHMANDU",
            "dollars": "55.30",
            "for": None,
            "category": "Travel",
            "waiting_on": "what it was for",
        }
    ],
}
NOTHING = {"person": "Ruth Hollis", "review": "Not yet sent for review", "empty": True}
BUTTON = {"who": "carol-jennings", "card": "4", "until": "0", "sig": "0" * 64}


def pages() -> dict[str, str]:
    title = "Ruth Hollis's October 2026"
    return {
        "receipt": receipts.page("n", ministry=MINISTRY),
        "catch-up": receipts.page("n", many=True, ministry=MINISTRY),
        "month": month_page.render(
            RUTH, title=title, when="October 2026", ministry=MINISTRY, approve=BUTTON, nonce="n"
        ),
        "month-empty": month_page.render(
            NOTHING, title=title, when="October 2026", ministry=MINISTRY, approve=None, nonce="n"
        ),
        "month-timed-out": month_page.MonthPage(
            desk=None, tools_for=print, ministry=MINISTRY
        )._note("This page has timed out", month_page._AGAIN, "n"),
    }


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, html in pages().items():
        (out / f"{name}.html").write_text(html, encoding="utf-8")
    print(len(pages()))
