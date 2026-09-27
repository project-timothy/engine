"""The fixture adapter: canned replies, no network, what every test uses.

Three ways to seed it, one per test shape:

- a ``dict`` keyed by job type (``"*"`` is the default key);
- a ``list`` of replies handed out in order (the retry tests);
- a callable ``(bundle, schema) -> RawReply | str`` for anything else.

Seed it with an exception INSTANCE and every call raises it (the transport
mapping tests). Every bundle and schema it sees is kept on ``calls`` and
``schemas`` so a test can assert what the gateway sent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.llm.gateway import GatewayTransportError, PromptBundle, RawReply, Usage

Seed = dict[str, str] | list[str] | Callable[[PromptBundle, dict[str, Any]], RawReply | str]


class FixtureAdapter:
    name = "fixture"

    def __init__(self, replies: Seed | BaseException, *, usage: Usage | None = None) -> None:
        self._seed = replies
        self._usage = usage or Usage()
        self._queue: list[str] = list(replies) if isinstance(replies, list) else []
        self.calls: list[PromptBundle] = []
        self.schemas: list[dict[str, Any]] = []

    def complete(self, bundle: PromptBundle, schema: dict[str, Any]) -> RawReply:
        self.calls.append(bundle)
        self.schemas.append(schema)
        seed = self._seed
        if isinstance(seed, BaseException):
            raise seed
        if isinstance(seed, dict):
            text = seed.get(bundle.job_type, seed.get("*"))
            if text is None:
                raise GatewayTransportError(
                    f"no fixture reply for job type {bundle.job_type!r}",
                    cause="fixture",
                    transient=False,
                )
            return RawReply(text=text, usage=self._usage)
        if isinstance(seed, list):
            if not self._queue:
                raise GatewayTransportError(
                    f"no fixture reply left for job type {bundle.job_type!r}",
                    cause="fixture",
                    transient=False,
                )
            return RawReply(text=self._queue.pop(0), usage=self._usage)
        result = seed(bundle, schema)
        if isinstance(result, str):
            return RawReply(text=result, usage=self._usage)
        return result
