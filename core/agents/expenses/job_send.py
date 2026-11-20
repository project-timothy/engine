"""The send job: claims go to the institution through the approval queue.
The institution approves and pays in its own system; the engine moves no money.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.runkey import RunKey
from ...institution import HELD, SENT, claim_sink, submit
from .common import _events_of, _person_slug
from .records import (
    CATEGORY_EVENT,
    SEND_CARD,
    SENT_EVENT,
    from_pay,
)
from .report_lines import (
    _fx_inputs,
    _is_door_receipt,
    _price_claims,
    _report_content_hash,
    _unconsumed,
)

HELD_EVENT = "institution.claim.held"


# ---- job: send --------------------------------------------------------------


def _claim_states(ctx: JobContext) -> dict[str, tuple[str, int]]:
    """Each receipt's latest send outcome and how many times it was held."""
    out: dict[str, tuple[str, int]] = {}
    for e in ctx.ledger.read_event_log():
        kind = e.get("event_type")
        if kind not in (SENT_EVENT, HELD_EVENT):
            continue
        sha = str(e.get("payload", {}).get("receipt", ""))
        held = out.get(sha, ("", 0))[1] + (kind == HELD_EVENT)
        out[sha] = (SENT if kind == SENT_EVENT else HELD, held)
    return out


def _words_by_sha(ctx: JobContext) -> dict[str, str]:
    """The person's own words for each receipt, as the door recorded them."""
    return {
        str(e["payload"]["sha256"]): str(e["payload"].get("said", ""))
        for e in _events_of(ctx, CATEGORY_EVENT)
        if e.get("payload", {}).get("sha256") and e["payload"].get("said")
    }


def _send_cards(ctx: JobContext, status: str) -> list[dict]:
    prefix = f"apr:{ctx.tenant_slug}:expenses:{SEND_CARD}:"
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM approval_queue WHERE tenant = ? AND action_type = ? AND status = ?",
        (ctx.tenant_slug, SEND_CARD, status),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "key": str(r["idempotency_key"]).removeprefix(prefix),
            "receipts": {
                s for s in json.loads(r["params_json"] or "{}").get("receipts", "").split(",") if s
            },
        }
        for r in rows
    ]


def _unsent(ctx: JobContext) -> list[dict]:
    states = _claim_states(ctx)
    mine = [i for i in _unconsumed(ctx) if states.get(i["sha256"], ("", 0))[0] != SENT]
    paid = from_pay(ctx.ledger.read_event_log())  # their own pay: no claim (c34 gap C)
    return [i for i in mine if i["sha256"] not in paid]


def _send_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expsend")
    key.config("institution", "expenses", "identity")
    items = _unsent(ctx)
    key.add("content", _report_content_hash(items))
    key.value("words", sorted(_words_by_sha(ctx).items()))
    key.value(
        "cards", sorted((c["id"], s) for s in ("pending", "approved") for c in _send_cards(ctx, s))
    )
    rates, pending_day = _fx_inputs(ctx, items)
    key.value("fx", rates)
    key.add("fx_pending_day", pending_day)
    # A held claim is tried again on the next run, never skipped as a re-run.
    states = _claim_states(ctx)
    held = any(states.get(i["sha256"], ("", 0))[0] == HELD for i in items)
    key.add("held_retry", datetime.now(UTC).isoformat() if held else "")
    return key.digest()


def _claim_of(item: dict, words: dict[str, str]):
    from ...contracts.institution import Claim

    name = str(item.get("file", ""))
    cents = item.get("amount_cents")
    usd = f"{Decimal(int(cents)) / 100:.2f}" if cents is not None else ""
    if item.get("said_currency"):
        amount, currency = str(item["said_amount"]), str(item["said_currency"])
    else:
        amount, currency = usd, "USD" if usd else ""
    return Claim(
        receipt=str(item["sha256"]),
        person=str(item.get("person", "")),
        day=name[:10] if _is_door_receipt(name) else "",
        amount=amount,
        currency=currency,
        usd=usd,
        said=words.get(str(item["sha256"]), ""),
        photo=str(item.get("filed_to") or name),
    )


def _claim_line(claim) -> str:
    """One receipt on the person's card, the way they said it."""
    if claim.currency and claim.currency != "USD":
        paid = f"{claim.currency} {Decimal(claim.amount):,.2f}"
    else:
        paid = f"${Decimal(claim.amount):,.2f}" if claim.amount else "amount not known yet"
    return f"{claim.day or 'undated'}: {paid}" + (f", {claim.said}" if claim.said else "")


def _send_run(ctx: JobContext) -> JobOutput:
    """A missionary's receipts to their institution (Robb's idea 1, decided
    2026-10-13). Receipts not yet sent wait on one card per person; their
    yes sends them on the next run, each claim as they said it, priced in
    dollars at its own day's rate when that rate is known. A claim the
    institution doesn't take is held in plain words and sent on a later run
    with no second yes. Every send is to the institution's own door, behind
    the person's card (invariant 7)."""
    institution = ctx.tenant.institution
    if not institution.submit_to:
        return JobOutput(status="ok", summary="expenses send: no institution takes claims here")
    who = institution.name or "the institution"
    unsent = {i["sha256"]: i for i in _unsent(ctx)}
    yes = set().union(*(c["receipts"] for c in _send_cards(ctx, "approved")))
    priced, events = _price_claims(ctx, [unsent[s] for s in sorted(unsent)])
    words = _words_by_sha(ctx)
    claims = {i["sha256"]: _claim_of(i, words) for i in priced}
    actions: list[str] = []

    ready = [claims[s] for s in sorted(yes) if s in claims]
    if ready and ctx.shadow:
        actions.append(f"would send {len(ready)} claim(s) to {who}")
    elif ready:
        sink = claim_sink(institution, store=ctx.ledger.root / "institution")
        states = _claim_states(ctx)
        for out in submit(sink, ready, institution=institution.name):
            if out.status == SENT:
                actions.append(f"sent {out.receipt[:12]} to {who} as {out.reference}")
                events.append(
                    EventSpec(
                        key=f"instsent:{out.receipt}",
                        event_type=SENT_EVENT,
                        payload={"receipt": out.receipt, "reference": out.reference},
                    )
                )
            else:
                n = states.get(out.receipt, ("", 0))[1]
                actions.append(f"held {out.receipt[:12]}: {who} isn't answering")
                events.append(
                    EventSpec(
                        key=f"instheld:{out.receipt}:{n}",
                        event_type=HELD_EVENT,
                        payload={"receipt": out.receipt, "say": out.say},
                    )
                )

    approvals = []
    by_person: dict[str, list] = {}
    for sha, claim in claims.items():
        if sha not in yes:
            by_person.setdefault(claim.person, []).append(claim)
    pending = _send_cards(ctx, "pending")
    for person, mine in sorted(by_person.items()):
        mine.sort(key=lambda c: (c.day, c.receipt))
        shas = {c.receipt for c in mine}
        digest = hashlib.sha256(",".join(sorted(shas)).encode()).hexdigest()[:16]
        n = len(mine)
        approvals.append(
            ApprovalSpec(
                key=f"instsend:{_person_slug(person)}:{digest}",
                action_type=SEND_CARD,
                params={
                    "person": person,
                    "institution": who,
                    "count": str(n),
                    "what": f"{n} receipt{'s' if n != 1 else ''} to send to {who}",
                    "receipts": ",".join(sorted(shas)),
                    "lines": "; ".join(_claim_line(c) for c in mine),
                },
                reason=f"{person}'s receipts not yet sent; approve to send them to {who}",
                supersedes_keys=[c["key"] for c in pending if c["receipts"] < shas],
            )
        )
    return JobOutput(
        status="ok",
        summary=f"expenses send: {len(ready)} to {who} on a yes, "
        f"{sum(len(m) for m in by_person.values())} waiting on a card",
        actions=actions,
        events=events,
        approvals=approvals,
    )
