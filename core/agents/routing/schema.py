"""Routing: the clock behind cards on a route (#436).

Boundary classification (docs/boundary-rules.md): **code**, row 1. Inputs are
the tenant's authority.toml (core/authority) and the approval queue; outputs
are ledger events only. No model, no send, no money.
"""

from __future__ import annotations

from pydantic import BaseModel


class Reminder(BaseModel):
    """One ``route.reminder`` event's payload."""

    card: int
    step: int
    level: int
    kind: str
    to: str
    due: str
    text: str


class DelegationRecord(BaseModel):
    """One ``route.delegated`` event's payload."""

    delegation: str
    by: str
    to: str
    role: str
    start: str
    until: str
