"""A ministry expense a person paid from their own pocket, one time (board
chunk c28; the owner, 2026-11-12: a board member paid the ministry's state
filing fee on their own card).

Anyone at the ministry, a board member or a director as much as the
missionary, can say they paid for something the ministry owed: the day,
the dollars, what it was for. It raises one card, decided as the ministry's
own safeguards say: at a solo ministry the director's own yes can do it; at
a church or an organization someone else says yes (the card names who paid
as its submitter). Once it is approved the
books read it as the ministry's spending, owed back to them. When the
ministry's bank pays them back, that line is explained as paying them back
(``pays_back`` on the line's purpose), and what they're owed comes down by
it.

Nothing here moves money: the card records a debt, and the payback is a
line the bank already shows.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

from ...authority import CardRule
from ...engine.contracts import ApprovalSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from .statements import OWED, PERSON

ADVANCE_CARD = "expenses.advance"
RULE = CardRule("approve", "expense.report", amount="cents", cents=True, submitter="person")
"""Approving it commits the ministry to paying it back. The card names who
paid as its submitter, so the ministry's own safeguards say whether they may
approve their own (the owner, 2026-11-12): a solo ministry's director can, a
church or an organization never lets them."""

APPROVED = (
    "SELECT id, params_json FROM approval_queue "
    "WHERE tenant = ? AND action_type = ? AND status = 'approved' ORDER BY id"
)
PENDING = APPROVED.replace("'approved'", "'pending'")

_FIELDS = ("person", "name", "day", "cents", "said", "category", "by")
"""``person`` is who paid, as the ministry's people list names them (the card's
submitter); ``name`` is how the books carry them."""


def key(ctx: JobContext) -> str:
    k = RunKey(ctx, "expadvance")
    for name in _FIELDS:
        k.param(name)
    k.add("rejected", _rejected(ctx, _ident(ctx.params)))
    return k.digest()


def _ident(params: dict) -> str:
    raw = json.dumps([str(params.get(n) or "") for n in ("person", "day", "cents", "said")])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _rejected(ctx: JobContext, ident: str) -> int:
    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE tenant = ? AND action_type = ? "
        "AND status = 'rejected'",
        (ctx.tenant_slug, ADVANCE_CARD),
    ).fetchall()
    return sum(1 for r in rows if json.loads(r[0] or "{}").get("ident") == ident)


def run(ctx: JobContext) -> JobOutput:
    """The card for one expense a person paid themselves. The same words
    again are the same card; one turned down is asked afresh."""
    p = {n: str(ctx.params.get(n) or "").strip() for n in _FIELDS}
    p["name"] = p["name"] or p["person"]
    if (
        not p["person"]
        or not p["said"]
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["day"])
        or not p["cents"].isdigit()
        or int(p["cents"]) <= 0
    ):
        return JobOutput(status="ok", summary="expenses advance: not an expense to record")
    ident = _ident(p)
    dollars = f"${int(p['cents']) / 100:,.2f}"
    what = f"{p['name']} paid {dollars} on {p['day']} for the ministry: {p['said']}"
    again = _rejected(ctx, ident)
    return JobOutput(
        status="ok",
        summary=f"expenses advance: {p['name']}'s {dollars} waits on a yes",
        approvals=[
            ApprovalSpec(
                key=f"advance:{ident}" + (f":reask-{again}" if again else ""),
                action_type=ADVANCE_CARD,
                params=p | {"ident": ident, "what": what},
                reason=f"{what}. A yes means the ministry owes it back to them.",
            )
        ],
    )


def approved(rows: Iterable) -> list[dict]:
    """The approved cards' params, each with its card id."""
    return [json.loads(r["params_json"] or "{}") | {"card": int(r["id"])} for r in rows]


def month_rows(cards: list[dict], month: str) -> dict[str, list[dict]]:
    """Each person's approved expenses in ``month``, as the month report's
    bank rows: the ministry's spending, owed back to them."""
    out: dict[str, list[dict]] = {}
    for c in cards:
        if str(c.get("day", ""))[:7] != month:
            continue
        cents = int(c["cents"])
        out.setdefault(str(c.get("name") or c["person"]), []).append(
            {
                "date": c["day"],
                "words": str(c["said"]),
                "dollars": f"{cents / 100:.2f}",
                "role": OWED,
                "account": None,
                "category": c.get("category") or None,
                "for": str(c["said"]),
                "waiting_on": None,
                "stands": "approved",
                "source": f"card#{c['card']}",
                "paid_from": PERSON,
                "paid_it_myself": True,
            }
        )
    return out
