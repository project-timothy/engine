"""Shared shape for 'does this ledger even have that table' (a tenant with
no AP book, or a brand-new ledger, is out of scope, not a crash)."""

from __future__ import annotations

from . import AuditContext


def has_table(ctx: AuditContext, name: str) -> bool:
    return bool(
        ctx.ledger.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    )


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"
