"""AP status vocabulary and transition rules.

The strings are the legacy ledger's own vocabulary (recorded in the fidelity
contract), used verbatim so shadow parity needs no mapping layer. They are
generic AP terms; no tenant is named.

The load-bearing fact: a payment entered into bill pay is COMMITTED money.
``Scheduled in bill pay`` sits between Received/Approved and Paid, and a row
in any committed status is never classified as payable. The absence of this
state nearly caused a ~$38K double-payment (2026-05-21); invariant 4 exists
because of it.
"""

from __future__ import annotations

# Needs scheduling; the only statuses a payment run may consider.
PAYABLE_STATUSES: frozenset[str] = frozenset({"Received", "Approved", "Outstanding"})

# Committed money: payment exists (check written or in the bill-pay queue)
# but has not cleared. Never payable.
COMMITTED_STATUSES: frozenset[str] = frozenset({"Scheduled", "Scheduled in bill pay"})

# Settled: cleared the bank, voided, or cancelled. Terminal.
SETTLED_STATUSES: frozenset[str] = frozenset(
    {"Paid", "Void - Already Paid", "Void - Duplicate", "Cancelled"}
)

ALL_STATUSES: frozenset[str] = PAYABLE_STATUSES | COMMITTED_STATUSES | SETTLED_STATUSES


class InvalidTransition(ValueError):
    pass


def is_payable_eligible(status: str) -> bool:
    return status in PAYABLE_STATUSES


def is_committed(status: str) -> bool:
    return status in COMMITTED_STATUSES


def is_settled(status: str) -> bool:
    return status in SETTLED_STATUSES


def assert_transition(status_from: str, status_to: str) -> None:
    """Validate a status flip. Forward-only along payable -> committed -> settled.

    Raises :class:`InvalidTransition` for unknown statuses, reverts out of a
    committed state, or any move out of a settled state. Settled corrections
    are a new row plus a void, never a silent reopen.
    """
    if status_from not in ALL_STATUSES:
        raise InvalidTransition(f"unknown status {status_from!r}")
    if status_to not in ALL_STATUSES:
        raise InvalidTransition(f"unknown status {status_to!r}")
    if is_settled(status_from):
        raise InvalidTransition(f"{status_from!r} is terminal; settled rows never reopen")
    if is_committed(status_from) and not (is_committed(status_to) or is_settled(status_to)):
        raise InvalidTransition(
            f"{status_from!r} is committed money; it can only settle, never revert to {status_to!r}"
        )
    if status_from == status_to:
        raise InvalidTransition(f"no-op transition {status_from!r} -> {status_to!r}")
