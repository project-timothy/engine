"""Deciding a card through the box's door: the one tool that writes (engine
#465, step 2b). Offered only over the HTTP door with a doorkeeper, and only
on a tenant whose authority.toml opened the door (``[presence] door``).

The signed-in person's AI holds their sign-in, so a decision that arrives
through the chat is the AI's until the person proves otherwise. The proof
is their own Face ID or fingerprint, checked by the doorkeeper:

1. The first call checks the person may decide this card at all (so their
   phone is never asked for something they cannot approve), asks the
   doorkeeper for a one-time link (core/tools/witness_desk.py), and hands
   it back: the person opens it, reads the card, taps Approve.
2. A later call collects the yes and decides the card through the same path
   the terminal uses (core/engine/queue_decide.py), stamped as decided by
   the door with the signed evidence. Until the tap, it is still waiting.

A collected yes is kept here until it is written, so a scheduled run
holding the books costs the person a "try again", never a second tap.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol

from ..authority import UNDESCRIBED, card_amount
from ..engine.authority_gate import (
    TenantAuthority,
    Witnessed,
    decide_card,
    load_delegations,
    load_policy,
)
from ..engine.queue_decide import decide_in_ledger
from ..engine.registry import load_card_authority
from ..engine.runner import LedgerLocked, ledger_write_lock
from ..ledger import Ledger
from .witness_desk import Asked, DeskRefused

if TYPE_CHECKING:
    from .catalog import Tools

DECISIONS = {"approve": "approved", "reject": "rejected"}


class Desk(Protocol):
    def ask(self, person: str, card: str, verb: str, summary: str) -> Asked: ...

    def check(self, witness: str) -> Witnessed | str: ...


@dataclass
class _Held:
    asked: Asked
    collected: Witnessed | None = None


class AskBook:
    """The links handed out and not yet decided, by (person, card, verb).
    One per door process; a restart forgets them, and the person is simply
    asked again."""

    def __init__(self) -> None:
        self._held: dict[tuple[str, int, str], _Held] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int, str]) -> _Held | None:
        with self._lock:
            return self._held.get(key)

    def put(self, key: tuple[str, int, str], held: _Held) -> None:
        with self._lock:
            self._held[key] = held

    def drop(self, key: tuple[str, int, str]) -> None:
        with self._lock:
            self._held.pop(key, None)


class Decider:
    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        ledger_root: Path,
        person: str,
        desk: Desk,
        asks: AskBook,
    ) -> None:
        self.tenant, self.ledger_root, self.person = tenant, Path(ledger_root), person
        self.desk, self.asks = desk, asks
        root = Path(tenants_root) if tenants_root is not None else None
        self.authority: TenantAuthority | None = load_policy(tenant, tenants_root=root)

    def offered(self) -> bool:
        return self.authority is not None and bool(self.authority.policy.door)

    def decide(self, t: Tools, args: dict) -> dict:
        card, verb = _card_of(args), str(args.get("decision", ""))
        if verb not in DECISIONS:
            raise ValueError("decision is approve or reject")
        row = self._waiting(t, card)
        key = (self.person, card, verb)
        held = self.asks.get(key)
        if held is not None and held.collected is None:
            answer = self.desk.check(held.asked.witness)
            if isinstance(answer, Witnessed):
                held.collected = answer
            elif answer == "waiting":
                return {
                    "status": "waiting",
                    "card": card,
                    "link": held.asked.link,
                    "say": "Still waiting for the tap. Open the link on your phone, read the "
                    "card, and tap the button; then tell me you're done.",
                }
            else:
                self.asks.drop(key)
                held = None
        if held is not None and held.collected is not None:
            return self._write(key, row, verb, held.collected)
        refused = self._refusal(t, row, verb, self.person)
        if refused:
            raise ValueError(plain_refusal(refused, row, verb))
        try:
            asked = self.desk.ask(self.person, str(card), verb, plain_words(row, verb))
        except DeskRefused as exc:
            voucher = self.authority.policy.vouch.get(self.person) if self.authority else None
            if exc.reason not in ("no_passkey", "not_invited") or not voucher:
                raise ValueError(_why_no_link(exc.reason)) from None
            return self._vouch(t, row, key, verb, voucher)
        self.asks.put(key, _Held(asked))
        return {
            "status": "tap_to_confirm",
            "card": card,
            "link": asked.link,
            "say": "Open this link on your phone, read the card, and tap the button. Face ID "
            "or your fingerprint shows it's you. Then tell me you're done and I'll finish it.",
        }

    def _waiting(self, t: Tools, card: int) -> dict:
        from .catalog import _card_visible

        rows = t._rows(
            "SELECT id, agent, action_type, params_json, status, created_at FROM approval_queue "
            "WHERE tenant = ? AND id = ? AND status = 'pending'",
            (self.tenant, card),
        )
        if not rows or not _card_visible(t, rows[0]):
            raise ValueError(f"no card #{card} is waiting for you")
        r = rows[0]
        return {
            "id": r["id"],
            "agent": r["agent"],
            "action_type": r["action_type"],
            "params": json.loads(r["params_json"] or "{}"),
            "status": r["status"],
            "created_at": r["created_at"],
        }

    def _vouch(
        self, t: Tools, row: dict, key: tuple[str, int, str], verb: str, voucher: str
    ) -> dict:
        """The person's phone cannot answer, so the person the ministry named
        for them confirms their answer with their own Face ID."""
        refused = self._refusal(t, row, verb, voucher)
        if refused:
            raise ValueError(plain_refusal(refused, row, verb))
        name, by = display_name(self.person), display_name(voucher)
        summary = f"{name} asked you to confirm their answer: {plain_words(row, verb)}"
        try:
            asked = self.desk.ask(voucher, str(row["id"]), verb, summary)
        except DeskRefused as exc:
            raise ValueError(_why_no_link(exc.reason)) from None
        self.asks.put(key, _Held(asked))
        return {
            "status": "tap_to_confirm",
            "card": row["id"],
            "link": asked.link,
            "vouched_by": by,
            "say": f"Your phone isn't set up for Face ID, so {by} confirms for you. Send {by} "
            "this link; they read the card and tap the button with their own Face ID. Then "
            "tell me it's done and I'll finish it.",
        }

    def _refusal(self, t: Tools, row: dict, verb: str, witness_by: str) -> str:
        """Would this answer, witnessed by ``witness_by``'s own Face ID (the
        person's, or their voucher's), decide the card? Asked before anyone's
        phone is."""
        if self.authority is None:
            return f"{self.tenant} has no authority.toml"
        con = t._db()
        try:
            delegations = load_delegations(SimpleNamespace(conn=con), self.tenant)
        finally:
            con.close()
        now = datetime.now(UTC)
        ruling = decide_card(
            self.authority.policy,
            self.person,
            row,
            decision=DECISIONS[verb],
            at_terminal=False,
            delegations=delegations,
            today=now.date(),
            now=now.isoformat(),
            witness=Witnessed(witness_by, str(row["id"]), verb, "", 0.0),
        )
        return ruling.refusal

    def _write(self, key: tuple[str, int, str], row: dict, verb: str, w: Witnessed) -> dict:
        card, decision = int(row["id"]), DECISIONS[verb]
        try:
            with ledger_write_lock(self.ledger_root), Ledger.open(self.ledger_root) as ledger:
                outcome = decide_in_ledger(
                    ledger,
                    self.tenant,
                    card,
                    decision,
                    authority=self.authority,
                    principal=self.person,
                    overrides={},
                    at_terminal=False,
                    confirm_human_only=lambda row, o: "a person decides it at a terminal",
                    witness=w,
                )
        except LedgerLocked:
            raise ValueError(
                "The books are busy with a scheduled run. Your answer is kept; try again in a "
                "minute."
            ) from None
        except (LookupError, ValueError) as exc:
            self.asks.drop(key)
            raise ValueError(plain_refusal(str(exc), row, verb)) from None
        self.asks.drop(key)
        if outcome.vote is not None:
            return {
                "status": "yes_recorded",
                "card": card,
                "say": f"Your yes is recorded. The card is now at {outcome.vote.step}, "
                f"waiting on {outcome.waiting}.",
            }
        return {
            "status": decision,
            "card": card,
            "action_type": outcome.action_type,
            "say": f"Card #{card} is {decision}.",
        }


def display_name(person: str) -> str:
    """``carol-jennings`` reads as "Carol Jennings"."""
    return " ".join(part.capitalize() for part in person.split("-") if part)


def _card_of(args: dict) -> int:
    card = args.get("card")
    if isinstance(card, bool):
        raise ValueError("card is the card's number")
    try:
        return int(str(card))
    except (TypeError, ValueError):
        raise ValueError("card is the card's number") from None


NOUNS = {
    "expense.report": "expense report",
    "ap.invoice": "bill",
    "payment": "payment",
    "vendor": "new vendor",
    "books": "change to the books",
    "purchase.order": "purchase order",
    "message": "message",
    "calendar": "calendar change",
    "authority": "change to who may do what",
    "card": "card",
}


def _rule_and_noun(row: dict):
    rule = load_card_authority(str(row["agent"])).get(str(row["action_type"]), UNDESCRIBED)
    return rule, NOUNS.get(rule.resource, "card")


def _a(noun: str) -> str:
    return f"{'an' if noun[0] in 'aeiou' else 'a'} {noun}"


def plain_words(row: dict, verb: str) -> str:
    """The card as the person reads it on the Approve page: what it is, who
    or what it is about, and the money it commits."""
    rule, noun = _rule_and_noun(row)
    params = row["params"]
    said = f"{verb.capitalize()} {_a(noun)}"
    who = str(params.get(rule.submitter, "")) if rule.submitter else ""
    vendor = str(params.get("vendor", ""))
    if who:
        said += f" for {who}"
    elif vendor:
        said += {"payment": " to ", "ap.invoice": " from "}.get(rule.resource, " named ") + vendor
    amount = card_amount(rule, params) if rule.money else None
    if amount is not None:
        said += f", ${amount:,.2f}"
    return f"{said} (card #{row['id']})."


_PREFIX = re.compile(r"^approval #\d+ (\([^)]*\) )?(refused for [^:]+: |refused: )?")

_PLAIN: tuple[tuple[str, str], ...] = (
    (
        r"human-only card",
        "This {noun} needs your own Face ID, or your ministry's own computer. "
        "Someone confirming for you can't decide it.",
    ),
    (r"is already (approved|rejected)", "Someone already decided this {noun}."),
    (
        r"does not let anyone approve their own",
        "You can't approve your own {noun}. Someone else on your ministry's list does that.",
    ),
    (
        r"approving your own is allowed only below (?P<limit>[\d.,]+)",
        "You can approve your own "
        "{noun} only below {limit}. Someone else on your ministry's list decides this one.",
    ),
    (
        r"a delegate never decides their own submission",
        "You can't decide something you submitted, even while you're covering for someone.",
    ),
    (
        r"already approved this card",
        "You've already said yes to this {noun}. The next yes has to come from someone else.",
    ),
    (
        r"vouched on this card",
        "You confirmed someone else's answer on this {noun}, so the next "
        "yes has to come from someone else.",
    ),
    (
        r"cannot vouch on their own card",
        "The person who confirms for you can't confirm {a_noun} "
        "about themselves. Someone else on your ministry's list will need to decide it.",
    ),
    (
        r"already voted on this card, so cannot also vouch",
        "The person who confirms for you "
        "already said yes to this {noun}, so they can't confirm yours too.",
    ),
    (
        r"decide[sd]? at a terminal",
        "This kind of card can't be decided from your phone yet. It's "
        "decided on your ministry's own computer.",
    ),
    (
        r"the witness is \S+'s, not",
        "That tap came from a phone that isn't yours, so nothing was "
        "decided. Ask Tim again and tap the link on your own phone.",
    ),
    (
        r"the witness (is for card|says)",
        "That tap was for a different answer, so nothing was decided. Ask Tim again.",
    ),
    (
        r"no grant lets|forbid .* wins|waits on|is not a person or agent",
        "You can't {verb} this "
        "{noun} through Tim. Someone else on your ministry's list decides it.",
    ),
)


def plain_refusal(reason: str, row: dict, verb: str) -> str:
    """The engine's exact reason, said the way a person reads it through the
    door: what happened and what to do, never an id, a rule or a file name.
    The terminal keeps the exact reason; a reason this table does not know
    still comes out plain."""
    _rule, noun = _rule_and_noun(row)
    reason = _PREFIX.sub("", reason)
    for pattern, said in _PLAIN:
        found = re.search(pattern, reason)
        if found:
            limit = found.groupdict().get("limit") or ""
            return said.format(noun=noun, a_noun=_a(noun), verb=verb, limit=_money(limit))
    return (
        f"Tim can't {verb} this {noun} for you. Whoever keeps your ministry's books can tell "
        "you why."
    )


def _money(text: str) -> str:
    try:
        return f"${Decimal(text.replace(',', '')):,.2f}" if text else ""
    except InvalidOperation:
        return text


def _why_no_link(reason: str) -> str:
    if reason in ("no_passkey", "not_invited"):
        return (
            "Your phone isn't set up to approve with Face ID yet. Ask your church for a "
            "welcome link, or decide this card at a terminal."
        )
    return "The box's door isn't answering right now; try again in a minute."


__all__ = ["AskBook", "Decider", "plain_refusal", "plain_words"]
