"""The model seam: the one method a provider adapter implements.

The gateway (``core/llm/gateway.py``) builds a ``PromptBundle``, hands it with
the output's JSON schema to an ``Adapter``, and validates what comes back;
the adapter only talks to its provider. ``core/llm/adapters/`` ships the
Anthropic Messages API, an OpenAI-compatible endpoint, and canned fixtures;
an adapter written elsewhere fits the same shape. A transport failure is a
``GatewayTransportError`` whose ``cause`` and ``transient`` the retry paths
read.

Shapes only (#344). The gateway re-exports every name, so code that imports
them from ``core.llm.gateway`` sees the same classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]

# ---- the prompt side ----------------------------------------------------------


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Attachment:
    """A file for the model to look at. The ADAPTER reads the bytes (base64
    inline on the wire); the gateway only carries the path and the MIME type.
    Images and PDFs are the shapes every adapter accepts."""

    path: Path
    mime: str


@dataclass(frozen=True)
class PromptBundle:
    """Everything an adapter needs for one provider call. ``messages`` may
    hold system turns anywhere; each adapter folds them into its provider's
    system slot. Attachments ride the FIRST user turn (the ask), which keeps
    them in place when the retry appends the correction turns."""

    job_type: str
    model: str
    messages: tuple[Message, ...]
    attachments: tuple[Attachment, ...]
    timeout_s: int

    def system_text(self) -> str:
        return "\n\n".join(m.content for m in self.messages if m.role == "system")

    def turns(self) -> tuple[Message, ...]:
        return tuple(m for m in self.messages if m.role != "system")


# ---- the reply side -----------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class RawReply:
    """What an adapter returns: the reply text as the provider gave it, the
    usage the provider reported, and the model id the provider reports back
    (``None`` when it reports nothing)."""

    text: str
    usage: Usage = Usage()
    model: str | None = None


class Adapter(Protocol):
    """One method: the prompt bundle plus the JSON schema in, raw text and
    usage out. Raise anything on a transport failure; the gateway maps it."""

    name: str

    def complete(self, bundle: PromptBundle, schema: dict[str, Any]) -> RawReply: ...


# ---- errors -------------------------------------------------------------------


class GatewayError(RuntimeError):
    """Base of every gateway failure."""


class GatewayTransportError(GatewayError):
    """The provider could not be reached or would not answer. ``cause`` is a
    short label (``timeout`` / ``transport_error`` / ``no_api_key`` /
    ``refusal`` / ``max_tokens``); ``transient`` says whether a redial could plausibly help,
    the same taxonomy the AP extractor's retry wrapper already reads."""

    def __init__(self, message: str, *, cause: str = "transport_error", transient: bool = True):
        super().__init__(message)
        self.cause = cause
        self.transient = transient
