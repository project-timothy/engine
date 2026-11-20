"""Deciding one card in the ledger: the one path the terminal
(``engine queue approve|reject``) and the box's door (the ``decide_card``
tool, core/tools/decide.py) share, so a decision means the same thing
however it arrived.

The caller holds the run lock and the open ledger. Under authority.toml the
card is decided on its route (``decide_card``): a vote with more to go is
kept on the card, which stays pending; the decision that resolves it runs
the agent's approval-time checks and records who decided and why. A
human-only card (#356) needs the person present: the caller's
``confirm_human_only`` at a terminal, or a door witness (their own Face ID,
or their named voucher's) on a resource the tenant opened with
``[presence] door``. Raises ``ValueError`` or
``LookupError`` with the reason; nothing is written then.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import clock
from .authority_gate import (
    DECISION_PARAMS,
    CardDecision,
    TenantAuthority,
    Witnessed,
    decide_card,
    load_delegations,
)
from .registry import load_approval_checks, load_human_only


@dataclass(frozen=True)
class Outcome:
    action_type: str
    overrides: dict[str, str]
    vote: CardDecision | None = None  # a yes kept on a route with more to go
    waiting: str = ""


def human_only(row: dict) -> bool:
    return row["action_type"] in load_human_only(str(row["agent"])) or (
        row["params"].get("human_only") == "true"
    )


def decide_in_ledger(
    ledger: Any,
    tenant: str,
    card_id: int,
    decision: str,
    *,
    authority: TenantAuthority | None,
    principal: str,
    overrides: dict[str, str],
    at_terminal: bool,
    confirm_human_only: Callable[[dict, dict[str, str]], str | None],
    witness: Witnessed | None = None,
    by_word: bool = False,
    said: str = "",
) -> Outcome:
    rows = ledger.list_approvals(tenant)
    row = next((r for r in rows if r["id"] == card_id), None)
    pending = row is not None and row["status"] == "pending"
    by_door = witness is not None and not at_terminal
    if by_door and authority is None:
        raise ValueError(f"{tenant} has no authority.toml, so nothing is decided through the door")
    vote = None
    if authority is not None and row is not None and pending:
        if not principal:
            raise ValueError(
                f"{tenant} decides cards through authority.toml: name who "
                "is deciding with --as <person or agent>"
            )
        now = clock.utc_now()
        ruling = decide_card(
            authority.policy,
            principal,
            row,
            decision=decision,
            at_terminal=at_terminal,
            delegations=load_delegations(ledger, tenant),
            today=now.date(),
            now=now.isoformat(),
            witness=witness,
            by_word=by_word,
            said=said,
            others=[r for r in rows if r["status"] == "pending" and r["id"] != card_id],
        )
        if ruling.refusal:
            raise ValueError(
                f"approval #{card_id} ({row['action_type']}) refused for "
                f"{principal}: {ruling.refusal}"
            )
        vote = None if ruling.final else ruling
        overrides.update(ruling.stamp or {})
    if row is not None and pending and human_only(row):
        if by_door and witness is not None:
            # decide_card above already held the witness to this person (or
            # the voucher named for them; the owner counts both the same),
            # this card, this answer, and a resource the door opens.
            overrides["decided_via"] = "door-vouched" if witness.person != principal else "door"
        else:
            refusal = confirm_human_only(row, overrides)
            if refusal:
                raise ValueError(refusal)
    if authority is not None and vote is not None and row is not None:
        ledger.update_pending_params(tenant, card_id, overrides)
        waiting = ", ".join(vote.waiting_on) or "the next approver"
        how = " by word" if by_word else ""
        if by_door and witness is not None:
            how = " by email" if witness.kind == "email" else " by Face ID"
        ledger.commit(
            agent="queue",
            job="route",
            idempotency_key=f"approval-{card_id}-vote-{principal}",
            summary=f"approval #{card_id}: {principal} said yes{how}; "
            f"{vote.step}, waiting on {waiting}",
        )
        return Outcome(row["action_type"], overrides, vote, waiting)

    def _check(agent: str, action_type: str, params: dict) -> str | None:
        fn = load_approval_checks(agent).get(action_type)
        return fn(ledger, tenant, params) if fn else None

    result = ledger.resolve_approval(
        tenant,
        card_id,
        decision,
        param_overrides=overrides,
        check=_check if decision == "approved" else None,
    )
    noted = f" ({', '.join(f'{k}={v}' for k, v in overrides.items())})" if overrides else ""
    ledger.commit(
        agent="queue",
        job=decision,
        idempotency_key=f"approval-{card_id}",
        summary=f"approval #{card_id} {decision}: {result['action_type']}{noted}",
    )
    return Outcome(result["action_type"], overrides)


TAKEN_BACK = (DECISION_PARAMS - {"handoff_to", "on_it_until", "on_it_set"}) | {
    "review_after",
    "self_approved",
    "vouchers",
    "decided_via",
    "vouched_by",
    "witness_at",
    "witness_kind",
    "witness_resource",
    "witness",
    "yes_by_word_up_to",
}
"""The params a decision stamps on a card (``authority_gate.decide_card``),
lifted off it when the decision is taken back and kept in its record."""
RAN_SINCE = "the books have run since it was decided, so it may already have been acted on"


def take_back_refusal(ledger: Any, tenant: str, row: dict, principal: str) -> str | None:
    """Why ``principal`` can't take back this card's decision, or None when
    they can: shared by :func:`take_back_in_ledger` and the button Tim puts
    up for words that come close, so a button never shows for a take-back
    its tap would refuse."""
    if row["status"] not in ("approved", "rejected"):
        return "it isn't decided, so there's nothing to take back"
    params = row["params"]
    if human_only(row):
        return "a person decides it at a terminal, so it's taken back there too"
    if params.get("decided_by") != principal:
        return "someone else decided it, so it's theirs to take back"
    if len(json.loads(params.get("route_votes") or "[]")) > 1:
        return "more than one yes went into it, so one can't be taken back alone"
    if ledger.ran_since(tenant, str(row["resolved_at"])):
        return RAN_SINCE
    return None


def take_back_in_ledger(
    ledger: Any, tenant: str, card_id: int, *, principal: str, said: str, via: str = "word"
) -> dict[str, str]:
    """A person takes back their own yes or no (the owner's call of
    2026-11-18, the persona year's A06): the card waits again, with who
    took it back, when, their words, and the decision it carried kept on it
    under ``taken_back``, so a run and the auditor see an ordinary waiting
    card. Only before any run of the tenant since the decision (the caller
    holds the write lock); never a human-only card, someone else's
    decision, or a card more than one yes went into
    (:func:`take_back_refusal`). ``via``: her ``word`` or her ``tap`` on
    the button Tim put up, kept on the record. Raises ``ValueError`` with
    the reason, ``LookupError`` for no such card."""
    row = next((r for r in ledger.list_approvals(tenant) if r["id"] == card_id), None)
    if row is None:
        raise LookupError(f"no approval #{card_id} for tenant {tenant!r}")
    why = take_back_refusal(ledger, tenant, row, principal)
    if why is not None:
        raise ValueError(why)
    params = dict(row["params"])
    stamp = {key: params.pop(key) for key in sorted(TAKEN_BACK) if key in params}
    if stamp.get("decided_via") == "door-word" and "said" in params:
        stamp["said"] = params.pop("said")  # the words that decided it, kept with it
    record = json.loads(params.get("taken_back") or "[]")
    record.append(
        {
            "by": principal,
            "at": clock.utc_now().isoformat(),
            "was": row["status"],
            "decided_at": row["resolved_at"],
            "said": said,
            "via": via,
            "stamp": stamp,
        }
    )
    params["taken_back"] = json.dumps(record, sort_keys=True)
    if not ledger.reopen_approval(tenant, card_id, params):
        raise ValueError(RAN_SINCE)
    ledger.commit(
        agent="queue",
        job="taken_back",
        idempotency_key=f"approval-{card_id}-taken-back-{len(record)}",
        summary=f"approval #{card_id} taken back by {principal}: it was {row['status']}, "
        f"and waits again ({row['action_type']})",
    )
    return {"action_type": str(row["action_type"]), "was": str(row["status"])}


__all__ = ["Outcome", "decide_in_ledger", "human_only", "take_back_in_ledger", "take_back_refusal"]
