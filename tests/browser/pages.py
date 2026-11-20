"""Every page the engine serves a person, rendered to files for the phone
photos CI keeps (structure review proposal 4): the receipt camera page, the
catch-up page, the receipt page reading a photo back (its question and her
answers), the month page (a full month with its Approve button, an
empty month, and the timed-out note), a missionary's own setup questions, and
Tim's own talk page (a conversation under her next thing, a first visit, and
the sign-in). Made-up content only.

    uv run python tests/browser/pages.py <out dir>
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from core.onboarding import interview
from core.tools import interview_page, month_page, receipts, talk_page

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
TALK = [
    {"said": "Hey Tim", "tim": "Hi Ruth. Your October report is ready to send for review."},
    {
        "said": "Here's a receipt, 4,300 rupees for the bus to Pokhara.",
        "tim": "Got it. Tap Add a receipt photo and snap it, and it goes in with October.",
    },
    {"said": "Done!", "tim": "Thanks, Ruth. It's in: 4,300 rupees, Travel, October 12."},
]
NEXT = {
    "say": "October is ready to send for review. Say the word and it goes.",
    "takes": "about a minute",
}


def _interview() -> dict[str, str]:
    """Hannah's own setup questions (board c49), each screen as she sees it."""
    book = interview.InterviewBook(Path(tempfile.mkdtemp()) / "interviews")
    token = book.mint(name="Hannah Cole", email="hannah@example.org", by="Paul Weston", now=0)
    steps = [
        ("support", {"support": ["own_nonprofit", "platform"]}),
        ("letter", {"legal_name": "Hope Springs Ministries Inc."}),
        ("where", {"town": "Nakuru, Kenya"}),
        ("reviewer", {"reviewer_name": "Sarah Whitfield", "reviewer_email": "sarah@example.org"}),
        ("driving", {"drives": "yes"}),
    ]
    out = {}
    for screen, form in steps:
        out[f"start-{screen}"] = interview_page._shell(
            "Setting up Tim", interview_page.screen_html(book, token, screen), "n"
        )
        book.take(token, screen, form, now=1)
    out["start-heard"] = interview_page._shell(
        "Setting up Tim", interview_page.screen_html(book, token, "heard"), "n"
    )
    # What her letter reads, for her tap (card R2).
    out["start-letter-read"] = interview_page._shell(
        "Setting up Tim",
        interview_page.read_back_html(token, "Hope Springs Ministries Inc.", "84-1234567"),
        "n",
    )
    return out


def _read_back(say: str, labels: list[str], *, even: bool = False) -> str:
    """The receipt page as it looks once Tim read the photo (card R1): his
    line, and her answers as buttons, drawn by the page's own script."""
    choices = json.dumps([{"pick": str(n), "label": label} for n, label in enumerate(labels)])
    ask = f"{{token:'t',even:{json.dumps(even)},choices:{choices}}}"
    shown = f"<script>say({json.dumps(say)});asked({ask});</script>"
    return receipts.page("n", ministry=MINISTRY, reads=True).replace("</body>", shown + "</body>")


def _statement_read(say: str) -> str:
    """The catch-up page once Tim read a statement that adds up (card R2):
    the pile's summary, and Keep them / No drawn by the page's own script."""
    choices = json.dumps([{"pick": "keep", "label": "Keep them"}, {"pick": "no", "label": "No"}])
    shown = (
        f"<script>say({json.dumps('All 1 sent. You can choose more, or go back to Tim.')}"
        f"+'\\n\\n'+{json.dumps(say)});asked({{token:'t',choices:{choices}}});</script>"
    )
    page = receipts.page("n", many=True, ministry=MINISTRY, statements=True)
    return page.replace("</body>", shown + "</body>")


def _pile_read(reads: list[str], unread: int) -> str:
    """The pile page once every photo is sent, with what Tim read on each
    listed for her one yes (the owner's read of 2026-11-19), drawn by the
    page's own script."""
    pushed = "".join(
        f"reads.push({{token:'t{n}',what:{json.dumps(what)}}});" for n, what in enumerate(reads)
    )
    sent = f"All {len(reads) + unread} sent. You can choose more, or go back to Tim."
    shown = (
        f"<script>say({json.dumps(sent)});"
        f"{pushed}unread={unread};summary();"
        "document.querySelector('ul.reads li button').click();</script>"
    )
    page = receipts.page("n", many=True, ministry=MINISTRY, reads=True)
    return page.replace("</body>", shown + "</body>")


def pages() -> dict[str, str]:
    title = "Ruth Hollis's October 2026"
    return {
        **_interview(),
        "receipt": receipts.page("n", ministry=MINISTRY),
        "catch-up": receipts.page("n", many=True, ministry=MINISTRY),
        "receipt-read": _read_back(
            "Got it. It reads Shoprite, November 12, 4,300 rupees. Right?",
            ["Yes, that's right", "No"],
        ),
        "catch-up-read": _pile_read(
            [
                "Shoprite, November 12, 4,300 rupees",
                "Bhatbhateni Supermarket, November 13, 1,250 rupees",
                "Pathao ride, November 14, 380 rupees",
                "Nepal Railways, 1,830 rupees (no day it was paid)",
            ],
            1,
        ),
        "catch-up-statement": _statement_read(
            "Your statement reads 8 lines, October 1, 2026 to October 31, 2026, starting at "
            "$2,418.36 and ending at $3,514.79. The lines add up to both. Keep them?"
        ),
        "receipt-read-which": _read_back(
            "The receipt shows $12.40, and you said $9. Which is right?",
            ["$12.40", "$9"],
            even=True,
        ),
        "month": month_page.render(
            RUTH,
            title=title,
            when="October 2026",
            ministry=MINISTRY,
            approve=BUTTON,
            nonce="n",
            card={"policy": "none"},
        ),
        "month-empty": month_page.render(
            NOTHING, title=title, when="October 2026", ministry=MINISTRY, approve=None, nonce="n"
        ),
        "talk": talk_page.render(TALK, nonce="n", ministry=MINISTRY, nxt=NEXT, photo=True),
        "talk-first": talk_page.render(
            [], nonce="n", ministry=MINISTRY, nxt={"say": "Nothing's waiting on you."}, photo=True
        ),
        "talk-sign-in": talk_page.sign_in_page("n", ministry=MINISTRY),
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
