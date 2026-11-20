"""Deciding a card through the box's door: the one tool that writes (engine
#465, step 2b). Offered only over the HTTP door with a doorkeeper, and only
on a tenant whose authority.toml opened the door (``[presence] door``).

The signed-in person's AI holds their sign-in, so a decision that arrives
through the chat is the AI's until the person proves otherwise. The proof
is their own Face ID or fingerprint, checked by the doorkeeper:

1. The call checks the person may decide this card at all (so their phone
   is never asked for something they cannot approve), asks the doorkeeper
   for a one-time link (core/tools/witness_desk.py), and hands it back: the
   person opens it, reads the card, taps Approve, Face ID.
2. The tap finishes it (engine #480, the owner's design day of 2026-10-11).
   The ``Finisher``, one thread beside the HTTP door, keeps a request open
   to the doorkeeper and hears each yes the moment it lands. It decides the
   card through the same path the terminal uses (core/engine/queue_decide.py),
   as the person the yes is for, stamped as decided by the door with the
   signed evidence, and tells the doorkeeper how it went; the person's
   Approve page shows that. Nobody goes back to the AI to say "done".

For a person with no passkey, the doorkeeper emails the link to the address
the ministry gave them and the engine hears only that it went (the owner's
decision of 2026-10-30): the tool tells the AI "Tim emailed you", with no
link to show, and the tap on the emailed link finishes it the same way.

The doorkeeper keeps a tapped yes until the finisher settles it, so a
scheduled run holding the books, or a restart, costs a delay, never the yes
or a second tap. Asked again, the tool says where the yes stands: the same
link (no rush), finishing, or already decided; a link that timed out is
replaced kindly.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol

from ..authority import UNDESCRIBED, card_amount, card_month, card_party
from ..engine.authority_gate import (
    BATCH,
    TenantAuthority,
    Witnessed,
    decide_card,
    load_delegations,
    load_policy,
    witnessed_cards,
)
from ..engine.queue_decide import (
    decide_in_ledger,
    human_only,
    take_back_in_ledger,
    take_back_refusal,
)
from ..engine.registry import load_card_authority
from ..engine.runner import DOOR_WAIT, LedgerLocked, ledger_write_lock
from ..ledger import Ledger
from .first_time import face_id_due, page_words
from .first_time import words as first_words
from .receipts import currency_name
from .viewer import display_name
from .witness_desk import Asked, DeskRefused, Tapped

if TYPE_CHECKING:
    from .catalog import Tools

Key = tuple[str, int | str, str]
"""(person, card or signed list, verb): one link at a time for each."""

DECISIONS = {"approve": "approved", "reject": "rejected"}
TAKE_BACK = "take_back"
"""decide_card's third decision: their own yes or no taken back, until the next
run acts on it (the owner's call of 2026-11-18)."""
TAKE_BACK_WORDS = re.compile(
    r"\b(?:undo|take\s+(?:\S+\s+){0,4}?back|taking\s+(?:\S+\s+){0,4}?back|reverse|"
    r"un-?approve|un-?reject|changed?\s+my\s+mind)\b"
)
"""Her own words asking for a take-back ("take back my yes on Ruth's report",
"undo that approval", "I changed my mind, reverse it")."""


def asks_to_take_back(said: str) -> bool:
    """Whether her own words ask to take a decision back."""
    text = " ".join(said.casefold().replace("\u2019", "'").split())
    return TAKE_BACK_WORDS.search(text) is not None


CLOSE_TO_TAKE_BACK = re.compile(
    r"\b(?:oops|whoops|my bad|by (?:mistake|accident)|a mistake|accidentally|"
    r"didn'?t mean|did not mean|shouldn'?t have|should not have|wasn'?t supposed to|"
    r"wrong (?:one|card|report|month|thing)|not that one|scratch that|revert|retract|"
    r"go back on|cancel (?:my|that|the) (?:yes|no|approval|rejection))(?![\w-])"
)
"""Words that come close to a take-back without asking for one ("oops, I
didn't mean to approve that"): Tim puts up a Take back button, and only her
tap does it (the owner's read of 2026-11-19)."""


def comes_close_to_take_back(said: str) -> bool:
    """Whether her words come close to taking a decision back."""
    text = " ".join(said.casefold().replace("\u2019", "'").split())
    return CLOSE_TO_TAKE_BACK.search(text) is not None


MOST_AT_ONCE = 10
"""Cards one tap decides; the doorkeeper's card field holds ten numbers."""
SAID = {"approved": "Approved", "rejected": "Declined"}


YOURS = "Yours to approve: your own yes decides it."
NOT_YOURS = "Someone else approves this one, not you."


def who_approves(yours: bool) -> str:
    """Who may approve, in the books' words, beside the ``yours`` flag (take 5
    of the persona year, A09: "you can approve it yourself, unless your board
    rules say otherwise, check that first"). Tim says it as given."""
    return YOURS if yours else NOT_YOURS


class Desk(Protocol):
    def ask(
        self, person: str, card: str, verb: str, summary: str, for_person: str | None = None
    ) -> Asked: ...

    def check(self, witness: str) -> str: ...

    def next(self, skip: Iterable[int], wait: float) -> list[Tapped]: ...

    def settle(self, yes: int, status: str, message: str) -> bool: ...


@dataclass
class _Held:
    asked: Asked


class AskBook:
    """The links handed out and not yet decided, by (person, card, verb).
    One per door process; a restart forgets them, and the person is simply
    asked again."""

    def __init__(self) -> None:
        self._held: dict[Key, _Held] = {}
        self._locks: dict[Key, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, key: Key) -> _Held | None:
        with self._lock:
            return self._held.get(key)

    def put(self, key: Key, held: _Held) -> None:
        with self._lock:
            self._held[key] = held

    def drop(self, key: Key) -> None:
        with self._lock:
            self._held.pop(key, None)

    def lock(self, key: Key) -> threading.Lock:
        """One call at a time per (person, card, verb): two calls at once
        hand out one link, never two (a second link retires the first)."""
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())


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

    def decide(self, t: Tools, args: dict, *, tap: bool = False) -> dict:
        """``tap``: always the person's own Face ID, never their word (the
        month page's Approve button, which no AI pressed; Tim's own page,
        where every card goes to the yes page), for one card or several."""
        verb = str(args.get("decision", ""))
        if verb == TAKE_BACK:
            return self._take_back(t, args, tap=tap)
        if verb not in DECISIONS:
            raise ValueError("decision is approve, reject, or take_back")
        said = str(args.get("said") or "")
        if args.get("cards") is not None:
            return self._decide_many(t, _cards_of(args), verb, said, tap)
        card = _card_of(args)
        key = (self.person, card, verb)
        with self.asks.lock(key):
            return self._decide(t, card, verb, key, said, tap)

    def take_back_offer(self, card: int) -> tuple[dict | None, str]:
        """For the Take back button Tim puts up when her words come close:
        the card in her terms (``what``) and her ``answer`` on it ("yes" or
        "no"), or None and why it can't be taken back, checked the way her
        tap would be (:func:`core.engine.queue_decide.take_back_refusal`)."""
        if self.authority is None:
            return None, "this ministry decides nothing through the door"
        with Ledger.open(self.ledger_root) as ledger:
            row = next((r for r in ledger.list_approvals(self.tenant) if r["id"] == card), None)
            if row is None:
                return None, f"there's no card {card}"
            why = take_back_refusal(ledger, self.tenant, row, self.person)
        if why is not None:
            return None, why
        answer = "yes" if row["status"] == "approved" else "no"
        return {"what": card_words(row), "answer": answer}, ""

    def _take_back(self, t: Tools, args: dict, *, tap: bool = False) -> dict:
        """Their own yes or no taken back, by their word at any amount: the
        safe direction, since the card only waits again
        (:func:`core.engine.queue_decide.take_back_in_ledger`). Never on a
        receipt's or a bank line's say-so: their own words must ask for it,
        or ``tap``, their own tap on the Take back button Tim put up (Tim's
        page, :func:`core.talk.loop.tapped`), which no model can press."""
        if args.get("cards") is not None:
            raise ValueError("take back one at a time, each by its own number")
        card = _card_of(args)
        said = str(args.get("said") or "")
        if not tap and not asks_to_take_back(said):
            raise ValueError(
                'only their own words asking to take it back do that ("take back my yes")'
            )
        if self.authority is None:
            raise ValueError("this ministry decides nothing through the door")
        try:
            with (
                ledger_write_lock(self.ledger_root, wait=DOOR_WAIT),
                Ledger.open(self.ledger_root) as ledger,
            ):
                take_back_in_ledger(
                    ledger,
                    self.tenant,
                    card,
                    principal=self.person,
                    said=said,
                    via="tap" if tap else "word",
                )
                row = next(r for r in ledger.list_approvals(self.tenant) if r["id"] == card)
        except LedgerLocked:
            raise ValueError(
                "a scheduled run holds the books right now and may act on it; ask again in a minute"
            ) from None
        return {
            "status": "taken_back",
            "card": card,
            "say": f"Taken back: {card_words(row)} waits for a yes or no again, and nothing "
            "ran on it. Tell them so in those words.",
        }

    def may_tap(self, t: Tools, card: int, verb: str = "approve") -> bool:
        """Whether this person's own Face ID would answer ``card``: it waits
        where they can see it and the rules let their yes count."""
        try:
            row = self._waiting(t, card)
        except ValueError:
            return False
        return not self._refusal(t, row, verb, self.person)

    def _decide(self, t: Tools, card: int, verb: str, key: Key, said: str, tap: bool) -> dict:
        try:
            row = self._waiting(t, card)
        except ValueError:
            done = self._already(t, card, verb)
            if done is None:
                raise
            self.asks.drop(key)
            return done
        held = self.asks.get(key)
        lapsed = False
        if held is not None:
            stands = self.desk.check(held.asked.witness)
            by_word = (
                self._by_word(t, row, verb, key, said) if stands == "waiting" and not tap else None
            )
            if by_word is not None:
                return by_word  # their words now name it; a tap on the old link hears it's done
            if stands == "waiting" and held.asked.emailed:
                return _emailed(card, verb, self.person, again=True)
            if stands == "waiting":
                return {
                    "status": "waiting",
                    "card": card,
                    "link": held.asked.link,
                    "show": _tap(verb, held.asked.link),
                    "say": "They haven't tapped yet. Tell them kindly there's no rush: the link "
                    "stays good for 30 minutes. Show the show line again as it is; when they "
                    "tap, the page finishes it.",
                }
            if stands == "witnessed":
                return {
                    "status": "finishing",
                    "card": card,
                    "say": "They've tapped; Tim is writing it now. Their page says when it's "
                    "done, and asking again in a moment shows it decided.",
                }
            self.asks.drop(key)  # settled without deciding it, or the link is gone
            held, lapsed = None, stands == "unknown"
        refused = self._refusal(t, row, verb, self.person)
        if refused:
            raise ValueError(plain_refusal(refused, row, verb))
        by_word = None if tap else self._by_word(t, row, verb, key, said)
        if by_word is not None:
            return by_word
        first = face_id_due(t)
        try:
            asked = self.desk.ask(
                self.person, str(card), verb, _first_words(plain_words(row, verb), first)
            )
        except DeskRefused as exc:
            voucher = self.authority.policy.vouch.get(self.person) if self.authority else None
            if exc.reason not in ("no_passkey", "not_invited") or not voucher:
                raise ValueError(_why_no_link(exc.reason)) from None
            return self._vouch(t, row, key, verb, voucher)
        self.asks.put(key, _Held(asked))
        if asked.emailed:
            return _emailed(card, verb, self.person, lapsed=lapsed)
        timed_out = (
            "Their last link timed out, which keeps the yes safe; this is a fresh one. "
            if lapsed
            else ""
        )
        return _first_face_id(
            t,
            first,
            {
                "status": "tap_to_confirm",
                "card": card,
                "link": asked.link,
                "show": _tap(verb, asked.link),
                "say": timed_out + "Show the person the show line as it is: a Markdown link, "
                "the label in brackets and the address inside the parentheses, so it shows as "
                "one short tap. Keep the address in it; without it there is nothing to tap. "
                "Never write the address on its own. They open it on their phone, read the "
                "card, and tap the button; Face ID or a fingerprint shows it's them, and the "
                "page finishes it and tells them it's done. Nothing more is needed from you or "
                "from them. If they later ask whether it went through, the same decide_card "
                "request tells you.",
            },
        )

    def _decide_many(self, t: Tools, cards: list[int], verb: str, said: str, tap: bool) -> dict:
        """Several cards on one link, one Face ID for all of them (chunk 7).
        Each card is held to the same rules as one card alone before any
        phone is asked; a card the person may not decide this way is left
        out and named, never dropped silently. Human-only cards always get
        their own tap: the auditor checks those signatures card by card."""
        later = cards[MOST_AT_ONCE:]
        rows: list[dict] = []
        done: list[str] = []
        left: list[str] = []
        for card in cards[:MOST_AT_ONCE]:
            try:
                row = self._waiting(t, card)
            except ValueError:
                already = self._already(t, card, verb)
                if already is not None:
                    done.append(already["say"].removeprefix("It's already done. "))
                else:
                    left.append("One of them isn't waiting for you.")
                continue
            if human_only(row):
                left.append(f"{_cap(card_words(row))} needs its own tap.")
                continue
            refused = self._refusal(t, row, verb, self.person)
            if refused:
                left.append(f"{_cap(card_words(row))}: {plain_refusal(refused, row, verb)}")
                continue
            rows.append(row)
        if later:
            left.append(
                f"Ten at a time: {len(later)} more wait for the next link once these are done."
            )
        if len(rows) == 1:
            key = (self.person, int(rows[0]["id"]), verb)
            with self.asks.lock(key):
                one = self._decide(t, int(rows[0]["id"]), verb, key, said, tap)
            return _with_the_rest(one, done, left)
        if not rows:
            reply: dict = {"status": "nothing_to_tap", "cards": [], "done": done}
            if left:
                reply["left_out"] = left
            reply["say"] = (
                "Nothing here needs their tap now. Tell them, in these words, what's done "
                "(done) and what isn't and why (left_out)."
                if left
                else "It's all done already. Tell them each one is done, in these words (done)."
            )
            return reply
        batch = BATCH + ".".join(str(r["id"]) for r in rows)
        key = (self.person, batch, verb)
        with self.asks.lock(key):
            return _with_the_rest(self._ask_many(t, rows, batch, key, verb), done, left)

    def _ask_many(self, t: Tools, rows: list[dict], batch: str, key: Key, verb: str) -> dict:
        ids = [int(r["id"]) for r in rows]
        held = self.asks.get(key)
        lapsed = False
        if held is not None:
            stands = self.desk.check(held.asked.witness)
            if stands == "waiting" and held.asked.emailed:
                return _emailed(ids, verb, self.person, again=True)
            if stands == "waiting":
                return {
                    "status": "waiting",
                    "cards": ids,
                    "link": held.asked.link,
                    "show": _tap_all(verb, len(rows), held.asked.link),
                    "say": "They haven't tapped yet. Tell them kindly there's no rush: the "
                    "link stays good for 30 minutes. Show the show line again as it is.",
                }
            if stands == "witnessed":
                return {
                    "status": "finishing",
                    "cards": ids,
                    "say": "They've tapped; Tim is writing them now. Their page says when "
                    "it's done, and asking again in a moment shows where each one stands.",
                }
            self.asks.drop(key)
            lapsed = stands == "unknown"
        asker, summary, for_person = self.person, batch_words(rows, verb), None
        first = face_id_due(t)
        try:
            asked = self.desk.ask(asker, batch, verb, _first_words(summary, first))
        except DeskRefused as exc:
            voucher = self.authority.policy.vouch.get(self.person) if self.authority else None
            if exc.reason not in ("no_passkey", "not_invited") or not voucher:
                raise ValueError(_why_no_link(exc.reason)) from None
            if any(self._refusal(t, r, verb, voucher) for r in rows):
                raise ValueError(
                    f"{display_name(voucher)} confirms for you, and can't confirm all of "
                    "these. Ask for them one at a time."
                ) from None
            asker, for_person = voucher, self.person
            summary = f"{display_name(self.person)} asked you to confirm their answer: {summary}"
            try:
                asked = self.desk.ask(asker, batch, verb, summary, for_person=for_person)
            except DeskRefused as again:
                raise ValueError(_why_no_link(again.reason)) from None
        self.asks.put(key, _Held(asked))
        if asked.emailed:
            return _emailed(ids, verb, asker, lapsed=lapsed, for_person=for_person)
        timed_out = (
            "Their last link timed out, which keeps the yes safe; this is a fresh one. "
            if lapsed
            else ""
        )
        by = (
            f"Their phone isn't set up for Face ID, so {display_name(asker)} confirms for "
            f"them: send {display_name(asker)} this link. "
            if for_person
            else ""
        )
        out = {
            "status": "tap_to_confirm",
            "cards": ids,
            "link": asked.link,
            "show": _tap_all(verb, len(rows), asked.link),
            "say": timed_out
            + by
            + f"One tap decides all {len(rows)}. Show the person the show line as it is: a "
            "Markdown link, the label in brackets and the address inside the parentheses. "
            "Never write the address on its own. The page lists every one in plain words; "
            "Face ID or a fingerprint shows it's them, and the page says which are done. If "
            "they later ask whether it went through, the same decide_card request tells you.",
        }
        if for_person:
            out["vouched_by"] = display_name(asker)
            return out
        return _first_face_id(t, first, out)

    def _by_word(self, t: Tools, row: dict, verb: str, key: Key, said: str) -> dict | None:
        """At or under the tenant's line, the person's word decides it, with no
        link and no tap (the owner's decision of 2026-10-11), when their own
        words name the card (2026-10-12). None sends them to the tap: over the
        line, human-only, no amount, words that name neither the person nor
        the amount, or a scheduled run holding the books (the tap then waits
        safely with the doorkeeper)."""
        if self._refusal(t, row, verb, self.person, by_word=True, said=said):
            return None
        status = DECISIONS[verb]
        try:
            out = write_answer(
                self.ledger_root,
                self.tenant,
                self.authority,
                self.person,
                int(row["id"]),
                status,
                by_word=True,
                said=said,
            )
        except LedgerLocked:
            return None
        self.asks.drop(key)
        if out.vote is not None:
            return {
                "status": "voted",
                "card": row["id"],
                "say": f"Their yes is in. It still waits on {out.waiting}. Tell them so in "
                "those words.",
            }
        return {
            "status": status,
            "card": row["id"],
            "say": f"Done. {done_words(row, status)} It's under the ministry's line, so their "
            "word decided it: no link, no tap. Tell them it's done, in those words.",
        }

    def _already(self, t: Tools, card: int, verb: str) -> dict | None:
        """The card is no longer waiting: if this person's own answer decided
        it (a "done" after the waiting call finished), say so plainly."""
        status = DECISIONS[verb]
        with Ledger.open(self.ledger_root) as ledger:
            rows = [r for r in ledger.list_approvals(self.tenant) if int(r["id"]) == card]
        if not rows or rows[0]["status"] != status:
            return None
        if rows[0]["params"].get("decided_by") != self.person:
            return None
        return {
            "status": status,
            "card": card,
            "say": f"It's already done. {done_words(rows[0], status)}",
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

    def _vouch(self, t: Tools, row: dict, key: Key, verb: str, voucher: str) -> dict:
        """The person's phone cannot answer, so the person the ministry named
        for them confirms their answer with their own Face ID."""
        refused = self._refusal(t, row, verb, voucher)
        if refused:
            raise ValueError(plain_refusal(refused, row, verb))
        name, by = display_name(self.person), display_name(voucher)
        summary = f"{name} asked you to confirm their answer: {plain_words(row, verb)}"
        try:
            asked = self.desk.ask(voucher, str(row["id"]), verb, summary, for_person=self.person)
        except DeskRefused as exc:
            raise ValueError(_why_no_link(exc.reason)) from None
        self.asks.put(key, _Held(asked))
        if asked.emailed:
            return _emailed(int(row["id"]), verb, voucher, for_person=self.person)
        return {
            "status": "tap_to_confirm",
            "card": row["id"],
            "link": asked.link,
            "vouched_by": by,
            "say": f"Your phone isn't set up for Face ID, so {by} confirms for you. Send {by} "
            "this link; they read the card and tap the button with their own Face ID, and "
            "that finishes it.",
        }

    def _refusal(
        self,
        t: Tools,
        row: dict,
        verb: str,
        witness_by: str,
        *,
        by_word: bool = False,
        said: str = "",
    ) -> str:
        """Would this answer, witnessed by ``witness_by``'s own Face ID (the
        person's, or their voucher's), or said by word (``by_word``), decide
        the card? Asked before anyone's phone is."""
        if self.authority is None:
            return f"{self.tenant} has no authority.toml"
        con = t._db()
        try:
            delegations = load_delegations(SimpleNamespace(conn=con), self.tenant)
        finally:
            con.close()
        others: list[dict] = []
        if by_word:  # words that fit two waiting cards say which (the gate)
            with Ledger.open(self.ledger_root) as ledger:
                others = [
                    r
                    for r in ledger.list_approvals(self.tenant)
                    if r["status"] == "pending" and r["id"] != row["id"]
                ]
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
            witness=None if by_word else Witnessed(witness_by, str(row["id"]), verb, "", 0.0),
            by_word=by_word,
            said=said,
            others=others,
        )
        return ruling.refusal


class Finisher:
    """Writes each yes the moment the person taps: one thread beside the HTTP
    door, holding a request open to the doorkeeper (engine #480). The
    decision goes through the terminal's own path as the person the yes is
    for, and the doorkeeper hears how it went, for the person's page. A yes
    the books are too busy for stays with the doorkeeper and comes back after
    ``wait``; nothing here needs to survive a restart."""

    wait = 25.0  # how long one open request waits for a tap
    pause = 5.0  # after the doorkeeper did not answer

    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        ledger_root: Path,
        desk: Desk,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tenant, self.ledger_root, self.desk, self.sleep = (
            tenant,
            Path(ledger_root),
            desk,
            sleep,
        )
        self.tenants_root = Path(tenants_root) if tenants_root is not None else None

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="door-finisher", daemon=True)
        thread.start()
        return thread

    def run_forever(self) -> None:
        held: set[int] = set()
        while True:
            held = self.step(held)

    def step(self, held: set[int]) -> set[int]:
        """One turn; a doorkeeper that is away, or anything unexpected, costs
        a pause, never the thread."""
        try:
            return self.run_once(frozenset(held))
        except DeskRefused:
            self.sleep(self.pause)
        except Exception as exc:  # noqa: BLE001  the finisher outlives one bad yes
            print(f"engine door: finisher: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.sleep(self.pause)
        return held

    def run_once(self, held: frozenset[int] = frozenset()) -> set[int]:
        """Hear the tapped yeses, write each, settle each; returns the ones
        the books were too busy for."""
        busy: set[int] = set()
        for tapped in self.desk.next(held, self.wait):
            went = self.finish(tapped)
            if went is None:
                busy.add(tapped.yes)
            else:
                self.desk.settle(tapped.yes, *went)
        return busy

    def finish(self, tapped: Tapped) -> tuple[str, str] | None:
        """(status, the page's words) once written, or None while a scheduled
        run holds the books."""
        w, person = tapped.witness, tapped.principal
        decision = DECISIONS.get(w.verb)
        if w.card.startswith(BATCH) and decision is not None:
            return self._finish_many(tapped, decision)
        row = self._card(w.card)
        if decision is None or row is None:
            return "refused", "That card isn't waiting any more."
        if row["status"] != "pending":
            if row["status"] == decision and row["params"].get("decided_by") == person:
                return decision, done_words(row, decision)  # written before a restart
            return "refused", plain_refusal(f"is already {row['status']}", row, w.verb)
        authority = load_policy(self.tenant, tenants_root=self.tenants_root)
        try:
            outcome = write_answer(
                self.ledger_root, self.tenant, authority, person, int(row["id"]), decision, w
            )
        except LedgerLocked:
            return None
        except (LookupError, ValueError) as exc:
            return "refused", plain_refusal(str(exc), row, w.verb)
        if outcome.vote is not None:
            return "recorded", (
                f"Your yes is recorded. The card is now at {outcome.vote.step}, waiting on "
                f"{outcome.waiting}."
            )
        return decision, done_words(row, decision)

    def _finish_many(self, tapped: Tapped, decision: str) -> tuple[str, str] | None:
        """One tap on a list: each card on it written through the same path
        as one card alone, and held to the rules again as it is written. A
        card decided meanwhile is named, never decided twice; a card added
        to the books after the link went out was never on the signed list.
        Safe to run again after a busy ledger or a restart: what this
        person already wrote reads as done."""
        w, person = tapped.witness, tapped.principal
        authority = load_policy(self.tenant, tenants_root=self.tenants_root)
        decided: list[dict] = []
        recorded: list[str] = []
        not_decided: list[str] = []
        for number in witnessed_cards(w.card):
            row = self._card(number)
            if row is None:
                not_decided.append("one card isn't there any more")
                continue
            if row["status"] != "pending":
                if row["status"] == decision and row["params"].get("decided_by") == person:
                    decided.append(row)
                else:
                    said = plain_refusal(f"is already {row['status']}", row, w.verb)
                    not_decided.append(f"{card_words(row)} ({_uncap(said)})")
                continue
            try:
                outcome = write_answer(
                    self.ledger_root, self.tenant, authority, person, int(row["id"]), decision, w
                )
            except LedgerLocked:
                return None
            except (LookupError, ValueError) as exc:
                said = plain_refusal(str(exc), row, w.verb)
                not_decided.append(f"{card_words(row)} ({_uncap(said)})")
                continue
            if outcome.vote is not None:
                recorded.append(f"{card_words(row)}, now waiting on {outcome.waiting}")
            else:
                decided.append(row)
        total = len(witnessed_cards(w.card))
        return _many_went(decision, total, decided, recorded, not_decided)

    def _card(self, card: str) -> dict | None:
        try:
            number = int(card)
        except ValueError:
            return None
        with Ledger.open(self.ledger_root) as ledger:
            rows = [r for r in ledger.list_approvals(self.tenant) if int(r["id"]) == number]
        return rows[0] if rows else None


def write_answer(
    ledger_root: Path,
    tenant: str,
    authority: TenantAuthority | None,
    person: str,
    card: int,
    decision: str,
    witness: Witnessed | None = None,
    *,
    by_word: bool = False,
    said: str = "",
):
    """The person's answer, witnessed by their Face ID or said by word under
    the tenant's line (``said``: their own words), decided through the terminal's own path
    (core/engine/queue_decide.py) under the ledger's write lock. Raises
    ``LedgerLocked`` while a scheduled run holds the books, and the engine's
    exact refusal as ``LookupError`` or ``ValueError``."""
    with ledger_write_lock(ledger_root, wait=DOOR_WAIT), Ledger.open(ledger_root) as ledger:
        return decide_in_ledger(
            ledger,
            tenant,
            card,
            decision,
            authority=authority,
            principal=person,
            overrides={},
            at_terminal=False,
            confirm_human_only=lambda row, o: "a person decides it at a terminal",
            witness=witness,
            by_word=by_word,
            said=said,
        )


def finisher_for(
    tenant: str, *, tenants_root: str | Path | None, ledger_root: Path, desk: Desk | None
) -> Finisher | None:
    """The finisher for a tenant whose authority.toml opened the door, with a
    doorkeeper to hear from; otherwise None."""
    root = Path(tenants_root) if tenants_root is not None else None
    authority = load_policy(tenant, tenants_root=root)
    if desk is None or authority is None or not authority.policy.door:
        return None
    return Finisher(tenant, tenants_root=root, ledger_root=ledger_root, desk=desk)


def _cards_of(args: dict) -> list[int]:
    """The card numbers asked for at once, each once, in the order given."""
    cards = args.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ValueError("cards is a list of the cards' numbers")
    seen: list[int] = []
    for card in cards:
        number = _card_of({"card": card})
        if number not in seen:
            seen.append(number)
    return seen


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


SUMMARY_CAP = 2000
"""The longest card the Approve page takes (the doorkeeper's MAX_SUMMARY)."""
_LINES_ROOM = SUMMARY_CAP - 300  # the headline, and a voucher's "X asked you to confirm"
_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def plain_words(row: dict, verb: str) -> str:
    """The card as the person reads it on the Approve page: what it is, who
    or what it is about, and the money it commits, then each receipt in it
    on its own line, so what they read is what their Face ID says yes to."""
    head = f"{verb.capitalize()} {card_words(row)}."
    lines = [_day_words(x) for x in str(row["params"].get("lines") or "").split("; ") if x]
    shown: list[str] = []
    for i, line in enumerate(lines):
        rest = len(lines) - i - 1
        tail = f"\n...and {rest} more receipts." if rest else ""
        if sum(len(s) + 1 for s in shown) + len(line) + 1 + len(tail) > _LINES_ROOM:
            shown.append(f"...and {len(lines) - i} more receipts.")
            break
        shown.append(line)
    already = str(row["params"].get("already") or "")
    if already:
        shown.append(f"{already}.")  # what this yes leaves alone
    stays = str(row["params"].get("stays") or "")
    if stays:
        shown.append(f"{stays}.")  # what this yes leaves in force
    moved = str(row["params"].get("moved") or "")
    if moved:
        # Money that moved between pockets, so the yes sees every line of
        # the month's statements, but owes nothing for it (t-statements).
        shown.append(f"Not spending: {_day_words(moved)}.")
    return "\n".join([head, *shown])


_CODED = re.compile(r"\b([A-Z]{3}) ([\d,]+)\.(\d{2})\b")


def _day_words(line: str) -> str:
    """A line as people say it: '2026-09-03: $42.10' reads 'Sep 3: $42.10',
    and so does a rate's day (the card names the month); 'NPR 4,300.00'
    reads '4,300 Nepali rupees' when the currency has a name people use."""

    def named(m: re.Match[str]) -> str:
        name = currency_name(m[1])
        cents = "" if m[3] == "00" else f".{m[3]}"
        return f"{m[2]}{cents} {name}" if name else m[0]

    def said(m: re.Match[str]) -> str:
        try:
            day = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return m[0]
        return f"{day:%b} {day.day}"

    return _CODED.sub(named, _ISO_DAY.sub(said, line))


def done_words(row: dict, decision: str) -> str:
    """What the person's page says once it is written."""
    stays = str(row["params"].get("stays") or "")
    return f"{SAID[decision]}: {card_words(row)}." + (f" {stays}." if stays else "")


_AN_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # how authority.toml names a person


def card_words(row: dict) -> str:
    """A card in the person's terms: what it is, whose, and how much
    ("an expense report for Ruth Hollis, $686.40")."""
    rule, noun = _rule_and_noun(row)
    params = row["params"]
    said = str(params.get("what") or "") or _a(noun)  # a card that says what it is
    who, vendor = card_party(rule, params)
    if who:  # in their name, never the id Tim keeps for them (the A01 spike, 2026-11-16)
        name = str(params.get("name") or "") or (
            display_name(who) if _AN_ID.fullmatch(who) else who
        )
        if name not in said:
            said += f" for {name}"
    elif vendor:
        said += {"payment": " to ", "ap.invoice": " from "}.get(rule.resource, " named ") + vendor
    month = card_month(params)
    if month:
        said += f", {month}"
    amount = card_amount(rule, params) if rule.money else None
    if amount is not None:
        said += f", ${amount:,.2f}"
    return said


def batch_words(rows: list[dict], verb: str) -> str:
    """Several cards as the person reads them on the Approve page: one line
    each, so what they read is what their one Face ID says yes to."""
    head = f"{verb.capitalize()} these {len(rows)}:"
    room = (SUMMARY_CAP - 300 - len(head)) // len(rows) - 3
    lines = [f"- {_clip(_cap(card_words(r)), room)}." for r in rows]
    return "\n".join([head, *lines])


def _clip(text: str, room: int) -> str:
    return text if len(text) <= room else text[: room - 3].rstrip() + "..."


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _uncap(text: str) -> str:
    return text[:1].lower() + text[1:].rstrip(".")


MAX_MESSAGE = 500
"""The longest outcome the doorkeeper's page takes (its MAX_MESSAGE)."""


def _many_went(
    decision: str, total: int, decided: list[dict], recorded: list[str], not_decided: list[str]
) -> tuple[str, str]:
    """(status, the page's words) for one tap on a list: how many were
    decided, then each one by name while there is room."""
    said = SAID[decision]
    if decided:
        head = (
            f"{said} all {total}" if len(decided) == total else f"{said} {len(decided)} of {total}"
        )
        status = decision
    elif recorded:
        head, status = f"Your yes is recorded on {len(recorded)}", "recorded"
    else:
        head, status = "None of these was decided", "refused"
    parts = [
        f"{head}: " + "; ".join(card_words(r) for r in decided) + "." if decided else head + "."
    ]
    if recorded:
        parts.append("Recorded: " + "; ".join(recorded) + ".")
    if not_decided:
        parts.append("Not decided: " + "; ".join(not_decided) + ".")
    message = " ".join(parts)
    if len(message) > MAX_MESSAGE:  # the names don't fit: the counts always do
        counts = [f"{head}."]
        if recorded and decided:
            counts.append(f"{len(recorded)} recorded, waiting on the next person.")
        if not_decided:
            counts.append(f"{len(not_decided)} not decided; ask Tim which.")
        message = " ".join(counts)
    return status, message


def _with_the_rest(reply: dict, done: list[str], left: list[str]) -> dict:
    """A reply about several cards also says what was already done and what
    was left out, and why, so the person hears about every card they named."""
    if done:
        reply["done"] = done
    if left:
        reply["left_out"] = left
    if done or left:
        reply["say"] += (
            " Also tell them, in these words, what was already done (done) and what isn't in "
            "this and why (left_out)."
        )
    return reply


def _emailed(
    cards: int | list[int],
    verb: str,
    asker: str,
    *,
    again: bool = False,
    lapsed: bool = False,
    for_person: str | None = None,
) -> dict:
    """The link went to ``asker``'s own email (no passkey): words for the AI,
    and nothing to show, since the AI never holds that link."""
    name = display_name(asker)
    word = "approve" if verb == "approve" else "decline"
    many = isinstance(cards, list) and len(cards) > 1
    what = f"all {len(cards)}" if many and isinstance(cards, list) else "it"
    if again:
        say = (
            f"Tim already emailed {name} the link. Tell them kindly there's no rush: it's in "
            "their email for 30 minutes, and their tap on it finishes it."
        )
    else:
        say = (
            (
                "Their last link timed out, which keeps the yes safe; Tim emailed a fresh one. "
                if lapsed
                else ""
            )
            + (
                f"Their phone isn't set up for Face ID, so {name} confirms for them. "
                if for_person
                else ""
            )
            + f"Tim emailed {name} a link to {word} {what}, at the address their ministry "
            "has. Tell them to open that email, on any phone or computer, and tap the button; "
            "the tap finishes it, and the page says it's done. There is no link for you to "
            "show: it went only to their email. If they later ask whether it went through, "
            "the same decide_card request tells you."
        )
    out: dict = {"status": "emailed", "say": say}
    out["cards" if isinstance(cards, list) else "card"] = cards
    if for_person:
        out["vouched_by"] = name
    return out


def _first_words(summary: str, first: bool) -> str:
    """The card's words, and the first time Face ID asks, its why and its
    rule after them for the yes page (core/tools/first_time.py), room
    allowing."""
    if not first:
        return summary
    more = page_words("face_id")
    return summary + more if len(summary) + len(more) <= SUMMARY_CAP else summary


def _first_face_id(t: Tools, first: bool, out: dict) -> dict:
    """A Face ID link's answer, carrying the first-time words the first time."""
    if first and t.shown.mark(t.viewer.person, "face_id"):
        return {**out, "first_time": first_words("face_id")}
    return out


def _tap_all(verb: str, n: int, link: str) -> str:
    label = "approve" if verb == "approve" else "decline"
    return f"[Tap to {label} all {n} with Face ID]({link})"


def _tap(verb: str, link: str) -> str:
    """The one-time link as a short tap, ready to show: the address alone is
    long enough to fill a phone's chat."""
    label = "approve" if verb == "approve" else "decline"
    return f"[Tap to {label} with Face ID]({link})"


_PREFIX = re.compile(r"^approval #\d+ (\([^)]*\) )?(refused for [^:]+: |refused: )?")

_PLAIN: tuple[tuple[str, str], ...] = (
    (
        r"human-only card",
        "This {noun} needs your own Face ID, or your ministry's own computer. "
        "Someone confirming for you can't decide it.",
    ),
    (r"is already (approved|rejected)", "Someone already decided this {noun}."),
    (
        r"so someone it does not cover decides it",
        "This covers you, so someone it doesn't cover decides it, like your board or your "
        "reviewer.",
    ),
    (
        r"so a director with no family tie decides it",
        "You're family of the person this pays, so a director with no family tie decides it.",
    ),
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
    if reason == "too_many":
        return "Tim has emailed you a lot this hour. Wait a little, then ask again."
    return "The box's door isn't answering right now; try again in a minute."


__all__ = [
    "AskBook",
    "Decider",
    "batch_words",
    "Finisher",
    "done_words",
    "finisher_for",
    "plain_refusal",
    "plain_words",
    "write_answer",
]
