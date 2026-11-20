"""The conversation, kept beside the books (Standalone Tim, shot 1).

One file per person: what they said, what Tim said, which tools ran. Kept 90
days, then dropped (the owner's decision of 2026-11-17), and never mailed.

It lives BESIDE the ledger, never inside it. The ledger is a git repository
and every run commits whatever sits under its root, so a file there is kept
forever; a conversation is meant to go. Private to the box's own account:
the folder is 0700 and each file 0600.

What a tool returned is not kept. It is the books, which are already on the
box, and the next turn reads them fresh. What is kept of a read is the
question Tim asked (the tool and its arguments, such as "October"; the
owner, 2026-11-20), so that turn can ask the books the same question.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..engine.private_file import keep as _keep
from ..engine.private_file import plain_name as _name

TALK_DIR = "talk"
KEEP_DAYS = 90

_LOCK = threading.Lock()  # the door answers on threads; one writer per process


def _folder(ledger_root: str | Path, tenant: str) -> Path:
    return Path(ledger_root).parent / TALK_DIR / _name(tenant)


class Conversation:
    """One person's conversation with Tim, newest last."""

    def __init__(self, ledger_root: str | Path, tenant: str, person: str) -> None:
        self.path = _folder(ledger_root, tenant) / f"{_name(person)}.jsonl"

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a crash mid-write
            if isinstance(entry, dict) and "said" in entry and "tim" in entry:
                out.append(entry)
        return out

    def _write(self, entries: list[dict[str, Any]]) -> None:
        _keep(self.path, "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))

    @staticmethod
    def _fresh(entries: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        return [e for e in entries if str(e.get("at", "")) >= cutoff]

    def recent(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """What was said in the last 90 days. Anything older leaves the file."""
        instant = now or datetime.now(UTC)
        with _LOCK:
            entries = self._read()
            kept = self._fresh(entries, instant)
            if len(kept) != len(entries):
                self._write(kept)
        return kept

    def add(
        self,
        said: str,
        tim: str,
        tools: list[str],
        *,
        now: datetime | None = None,
        status: str = "ok",
        near: bool = False,
        made: int | None = None,
        reads: list[dict[str, Any]] | None = None,
        tap: dict[str, str] | None = None,
    ) -> None:
        """One exchange: their words, Tim's words, the tools that ran;
        ``reads``, each read behind Tim's words as the question he asked
        the books (the tool and its arguments, never what it returned), so
        his next reply in the same sitting can ask again (``core.talk.loop``);
        ``tap``, her Yes or Not now on a button and what it named, so her
        page keeps it as one dated line (t-hark-patterns, 2026-11-20);
        ``near`` when Tim said he is near his month's limit (card V3);
        ``made``, the card a write in it left waiting on them, which their
        next words may decide (``core.talk.loop``)."""
        instant = now or datetime.now(UTC)
        entry: dict[str, Any] = {
            "at": instant.astimezone(UTC).isoformat(timespec="seconds"),
            "said": said,
            "tim": tim,
            "tools": list(tools),
            "status": status,
        }
        if near:
            entry["near"] = True
        if made is not None:
            entry["made"] = made
        if tap:
            entry["tap"] = dict(tap)
        if reads:
            entry["reads"] = [{"tool": r["tool"], "args": r["args"]} for r in reads]
        with _LOCK:
            self._write([*self._fresh(self._read(), instant), entry])


CONFIRM_LIFE = timedelta(minutes=15)
"""How long a Yes button Tim offers stays good."""
YES_PAGE_LIFE = timedelta(minutes=30)
"""How long the button to her yes page shows: the doorkeeper's link life."""
LINK_LIFE = timedelta(minutes=15)
"""How long a link Tim hands her (a receipt's camera) shows as a button:
the receipt link's own life."""


class Waiting:
    """The one thing on her page waiting on her own tap (card V2), beside
    her conversation and never in the ledger: a call Tim would make, held
    with its exact arguments and the plain line code wrote for it, or the
    link to her yes page. One at a time; a newer one replaces it. A held
    call is used once and lives 15 minutes."""

    def __init__(self, ledger_root: str | Path, tenant: str, person: str) -> None:
        self.path = _folder(ledger_root, tenant) / f"{_name(person)}.waiting.json"

    def _put(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            _keep(self.path, json.dumps(entry, sort_keys=True))
        return entry

    def hold(self, tool: str, args: dict, summary: str, *, now: datetime) -> dict[str, Any]:
        """Hold one call for her tap; returns what the page shows."""
        return self._put(
            {
                "kind": "confirm",
                "id": secrets.token_urlsafe(18),
                "tool": tool,
                "args": args,
                "summary": summary,
                "until": (now + CONFIRM_LIFE).astimezone(UTC).isoformat(timespec="seconds"),
            }
        )

    def yes_page(
        self,
        link: str,
        *,
        now: datetime,
        card: Any = None,
        ask: dict[str, Any] | None = None,
        asked: str = "",
    ) -> dict[str, Any]:
        """The button to her own yes page, for as long as its link lives.
        ``card``: the card (or cards) it decides, so her page drops the
        button once none of them is waiting any more. ``ask``: the
        decide_card request that made it, and ``asked`` when she first
        asked (now, unless this is her button back), so code can put her
        yes button back once (``core.talk.loop``); a button back carries
        ``again``."""
        until = (now + YES_PAGE_LIFE).astimezone(UTC).isoformat(timespec="seconds")
        entry: dict[str, Any] = {"kind": "yes_page", "link": link, "until": until}
        cards = card if isinstance(card, list) else [card] if card not in (None, "") else []
        if cards:
            entry["cards"] = [str(c) for c in cards]
        if ask is not None:
            entry["ask"] = ask
            entry["asked"] = asked or now.astimezone(UTC).isoformat(timespec="seconds")
            if asked:
                entry["again"] = True
        return self._put(entry)

    def link(self, text: str, link: str, *, now: datetime) -> dict[str, Any]:
        """A link a tool made for her (a receipt's camera), shown as a button
        under Tim's message instead of written into his words."""
        until = (now + LINK_LIFE).astimezone(UTC).isoformat(timespec="seconds")
        return self._put({"kind": "link", "text": text, "link": link, "until": until})

    def _read(self) -> dict[str, Any] | None:
        try:
            entry = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return entry if isinstance(entry, dict) else None

    def shown(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """What her page shows now, while it is still good."""
        instant = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        with _LOCK:
            entry = self._read()
        if entry is None or str(entry.get("until", "")) < instant:
            return None
        return entry

    def last(self) -> dict[str, Any] | None:
        """What her page last showed under Tim's message, good or not: what
        her tap on it did is told to Tim on her next message."""
        with _LOCK:
            return self._read()

    def take(self, given: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Her tap on a held call: the call, once, while it is good and the
        id is the one her page showed; None otherwise. A tap on an older
        button leaves the newer one standing."""
        instant = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        with _LOCK:
            entry = self._read()
            if entry is None or entry.get("kind") != "confirm":
                return None
            if not hmac.compare_digest(str(entry.get("id", "")), str(given)):
                return None
            self.path.unlink(missing_ok=True)  # once
        if str(entry.get("until", "")) < instant:
            return None
        return entry

    def drop(self) -> None:
        with _LOCK:
            self.path.unlink(missing_ok=True)
