"""The engine's half of the witness: asking the box's doorkeeper for a
person's own yes on one card, and collecting it (engine #465, step 2).

The doorkeeper (github.com/project-timothy/doorkeeper) is its own program
on the Tim box, never part of the engine. The engine asks it, over
localhost with the box's shared secret, for a yes from one person on one
card, with the card in plain words. It gets back a witness id, which it
keeps, and a one-time link, which goes to the person: they read the card,
tap Approve, and Face ID or a fingerprint proves it was them and not their
AI.

The tap finishes it (engine #480): the engine's finisher keeps one request
open (``next``) and hears each yes the moment it lands, with the signed
evidence; it writes the decision and says how it went (``settle``), which
the person's Approve page shows. The doorkeeper keeps the yes until then.

Anything the engine cannot read as a yes is no yes. Standard library only
(invariant 9).
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..engine import web
from ..engine.authority_gate import Witnessed
from .mcp_http import service_secret, service_url


class DeskRefused(Exception):
    """No link could be made: ``reason`` is the doorkeeper's word for why
    (``not_invited``, ``no_passkey``, ``invalid_request``) or
    ``unreachable``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Asked:
    witness: str  # the engine keeps this, to collect with
    link: str  # the person opens this, on their phone


@dataclass(frozen=True)
class Tapped:
    """A yes the person tapped, waiting to be written: ``yes`` names it when
    the engine says how it went; ``principal`` is whose yes it is (the
    witness's own person, unless a voucher tapped for them)."""

    yes: int
    principal: str
    witness: Witnessed


STANDINGS = ("waiting", "witnessed", "settled", "unknown")


class WitnessDesk:
    def __init__(self, *, url: str, secret_file: str | Path, timeout: float = 5.0) -> None:
        self.url = service_url(url).rstrip("/")
        self._secret = service_secret(secret_file)
        self.timeout = timeout

    def _post(
        self, path: str, form: dict[str, str], timeout: float | None = None
    ) -> tuple[int, Any]:
        request = web.request(
            self.url + path,
            data=urlencode(form).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._secret}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with web.urlopen(request, timeout=timeout or self.timeout) as resp:
                code, raw = resp.status, resp.read(1 << 20)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read(65536))
            except ValueError:
                return exc.code, None
        except (OSError, ValueError):
            return 0, None
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, None

    def ask(
        self, person: str, card: str, verb: str, summary: str, for_person: str | None = None
    ) -> Asked:
        """A one-time link for ``person`` to say ``verb`` on ``card``; with
        ``for_person``, they confirm that person's answer (the vouch)."""
        form = {"person": person, "card": card, "verb": verb, "summary": summary}
        if for_person:
            form["for"] = for_person
        status, reply = self._post("/witness/ask", form)
        if status == 200 and isinstance(reply, dict):
            witness, link = reply.get("witness"), reply.get("link")
            if isinstance(witness, str) and witness and isinstance(link, str) and link:
                return Asked(witness, link)
        if status == 400 and isinstance(reply, dict) and isinstance(reply.get("error"), str):
            raise DeskRefused(reply["error"])
        raise DeskRefused("unreachable")

    def check(self, witness: str) -> str:
        """Where the yes behind ``witness`` stands: "waiting" (no tap yet),
        "witnessed" (tapped, being written), "settled", or "unknown" (gone,
        or nothing the engine can read)."""
        status, reply = self._post("/witness/check", {"witness": witness})
        found = reply.get("status") if status == 200 and isinstance(reply, dict) else None
        return found if found in STANDINGS else "unknown"

    def next(self, skip: Iterable[int], wait: float) -> list[Tapped]:
        """Every tapped yes not yet written, as soon as there is one outside
        ``skip`` (the ones being retried), or all of them after ``wait``
        seconds. Raises ``DeskRefused("unreachable")`` when the doorkeeper is
        away, so the finisher pauses instead of spinning."""
        form = {"wait": f"{wait:g}", "skip": ",".join(str(n) for n in sorted(skip))}
        status, reply = self._post("/witness/next", form, timeout=wait + self.timeout)
        if status == 0:
            raise DeskRefused("unreachable")
        yeses = reply.get("yeses") if status == 200 and isinstance(reply, dict) else None
        if not isinstance(yeses, list):
            return []
        return [t for t in (_tapped(y) for y in yeses if isinstance(y, dict)) if t]

    def settle(self, yes: int, status: str, message: str) -> bool:
        """How it went, for the person's Approve page: approved, rejected,
        recorded (the card waits on the next person) or refused."""
        code, _ = self._post(
            "/witness/settled", {"yes": str(yes), "status": status, "message": message}
        )
        return code == 200

    def spend_month(self, grant: str) -> tuple[str, str, str] | None:
        """(reviewer, person, month) behind a grant the reviewer's Face ID
        minted on the month page, once (core/tools/month_page.py)."""
        status, reply = self._post("/month/grant", {"grant": grant})
        if status != 200 or not isinstance(reply, dict):
            return None
        found = tuple(reply.get(k) for k in ("reviewer", "person", "month"))
        if not all(isinstance(v, str) and v for v in found):
            return None
        reviewer, person, month = (str(v) for v in found)
        return reviewer, person, month


def _tapped(reply: dict) -> Tapped | None:
    yes, principal = reply.get("yes"), reply.get("for")
    if not isinstance(yes, int) or isinstance(yes, bool):
        return None
    if not isinstance(principal, str) or not principal:
        return None
    witness = _witnessed(reply)
    return Tapped(yes, principal, witness) if witness else None


def _witnessed(reply: dict) -> Witnessed | None:
    texts = [reply.get(k) for k in ("person", "card", "verb", "summary")]
    if not all(isinstance(t, str) and t for t in texts):
        return None
    at, evidence = reply.get("at"), reply.get("evidence")
    if not isinstance(at, int | float) or isinstance(at, bool) or not isinstance(evidence, dict):
        return None
    person, card, verb, summary = (str(t) for t in texts)
    return Witnessed(person, card, verb, summary, float(at), evidence)


__all__ = ["Asked", "DeskRefused", "Tapped", "WitnessDesk"]
