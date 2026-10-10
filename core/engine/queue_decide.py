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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .authority_gate import (
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
) -> Outcome:
    row = next((r for r in ledger.list_approvals(tenant) if r["id"] == card_id), None)
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
        now = datetime.now(UTC)
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
        how = " by Face ID" if by_door else ""
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


__all__ = ["Outcome", "decide_in_ledger", "human_only"]
