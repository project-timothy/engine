"""The engine's half of the witness: asking the box's doorkeeper for a
person's own yes on one card, and collecting it (engine #465, step 2).

The doorkeeper (github.com/project-timothy/doorkeeper) is its own program
on the Tim box, never part of the engine. The engine asks it, over
localhost with the box's shared secret, for a yes from one person on one
card, with the card in plain words. It gets back a witness id, which it
keeps, and a one-time link, which goes to the person: they read the card,
tap Approve, and Face ID or a fingerprint proves it was them and not their
AI. The engine then collects the yes, once, with the signed evidence.

Anything the engine cannot read as a yes is no yes. Standard library only
(invariant 9).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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


class WitnessDesk:
    def __init__(self, *, url: str, secret_file: str | Path, timeout: float = 5.0) -> None:
        self.url = service_url(url).rstrip("/")
        self._secret = service_secret(secret_file)
        self.timeout = timeout

    def _post(self, path: str, form: dict[str, str]) -> tuple[int, Any]:
        request = urllib.request.Request(  # noqa: S310  scheme checked in __init__
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
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read(65536))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read(65536))
            except ValueError:
                return exc.code, None
        except (OSError, ValueError):
            return 0, None

    def ask(self, person: str, card: str, verb: str, summary: str) -> Asked:
        """A one-time link for ``person`` to say ``verb`` on ``card``."""
        form = {"person": person, "card": card, "verb": verb, "summary": summary}
        status, reply = self._post("/witness/ask", form)
        if status == 200 and isinstance(reply, dict):
            witness, link = reply.get("witness"), reply.get("link")
            if isinstance(witness, str) and witness and isinstance(link, str) and link:
                return Asked(witness, link)
        if status == 400 and isinstance(reply, dict) and isinstance(reply.get("error"), str):
            raise DeskRefused(reply["error"])
        raise DeskRefused("unreachable")

    def check(self, witness: str) -> Witnessed | str:
        """The person's answer, once: a ``Witnessed``, or ``"waiting"`` (not
        yet), or ``"unknown"`` (gone, spent, or nothing the engine can read)."""
        status, reply = self._post("/witness/check", {"witness": witness})
        if status != 200 or not isinstance(reply, dict):
            return "unknown"
        if reply.get("status") == "waiting":
            return "waiting"
        return _witnessed(reply) or "unknown"


def _witnessed(reply: dict) -> Witnessed | None:
    if reply.get("status") != "witnessed":
        return None
    texts = [reply.get(k) for k in ("person", "card", "verb", "summary")]
    if not all(isinstance(t, str) and t for t in texts):
        return None
    at, evidence = reply.get("at"), reply.get("evidence")
    if not isinstance(at, int | float) or isinstance(at, bool) or not isinstance(evidence, dict):
        return None
    person, card, verb, summary = (str(t) for t in texts)
    return Witnessed(person, card, verb, summary, float(at), evidence)


__all__ = ["Asked", "DeskRefused", "WitnessDesk"]
