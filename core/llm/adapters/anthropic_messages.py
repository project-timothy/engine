"""The Anthropic Messages API adapter, over stdlib ``urllib`` (no new
dependency; invariant 9).

An attachment with no text layer (a scan) is RENDERED to page images by the
engine first (:mod:`core.llm.rasterize`, issue #296) and travels as one image
block per page plus a line of prose saying which document they came from. The
Messages API can rasterize a PDF server-side, so this is not what makes a scan
work here the way it is for the local tier; it makes every adapter send the
same thing, and it is the only shape a provider without a PDF reader can use.

Structured output rides the request's ``output_config.format`` block
(``{"type": "json_schema", "schema": ...}``; no beta header as of the
2026-09 docs at platform.claude.com/docs/en/build-with-claude/structured-outputs),
and the reply JSON comes back in the first text block. Constrained decoding
accepts a narrower JSON Schema than pydantic emits: every object must carry
``additionalProperties: false`` and numeric or length bounds are rejected,
so :func:`constrained_schema` tightens the schema on the way out. Pydantic
still enforces the bounds on the way back; nothing is lost.

The API key comes from the environment variable NAMED by ``api_key_env``;
the name is tenant config, the value never is (CLAUDE.md, secrets rule).
"""

from __future__ import annotations

import base64
import copy
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.llm.gateway import Attachment, GatewayTransportError, PromptBundle, RawReply, Usage
from core.llm.rasterize import render_attachment

DEFAULT_ENDPOINT = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# JSON Schema keywords the constrained decoder rejects (docs, 2026-09).
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
        "uniqueItems",
    }
)


def constrained_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic's JSON schema, tightened to what constrained decoding accepts."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS}
            if out.get("type") == "object" or "properties" in out:
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(schema))


def _block(mime: str, data: bytes) -> dict[str, Any]:
    return {
        "type": "document" if mime == "application/pdf" else "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _attachment_blocks(attachment: Attachment) -> list[dict[str, Any]]:
    """The file, or the page images the engine rendered from it when it has no
    text layer. A render that could not happen returns nothing, and the file
    goes over exactly as it did before."""
    rendered = render_attachment(attachment)
    if rendered is None:
        return [_block(attachment.mime, attachment.path.read_bytes())]
    return [_block(rendered.mime, page) for page in rendered.pages] + [
        {"type": "text", "text": rendered.note()}
    ]


def _messages(bundle: PromptBundle) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending = list(bundle.attachments)
    for turn in bundle.turns():
        blocks: list[dict[str, Any]] = []
        if turn.role == "user" and pending:
            for attachment in pending:
                blocks.extend(_attachment_blocks(attachment))
            pending = []
        blocks.append({"type": "text", "text": turn.content})
        out.append({"role": turn.role, "content": blocks})
    return out


class AnthropicMessagesAdapter:
    name = "anthropic_messages"

    def __init__(
        self,
        *,
        api_key_env: str,
        endpoint: str = DEFAULT_ENDPOINT,
        max_tokens: int = 4096,
    ) -> None:
        self._api_key_env = api_key_env
        self._endpoint = endpoint
        self._max_tokens = max_tokens

    def complete(self, bundle: PromptBundle, schema: dict[str, Any]) -> RawReply:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise GatewayTransportError(
                f"environment variable {self._api_key_env} is not set; no key, no call",
                cause="no_api_key",
                transient=False,
            )
        body = {
            "model": bundle.model,
            "max_tokens": self._max_tokens,
            "temperature": 0,
            "system": bundle.system_text(),
            "messages": _messages(bundle),
            "output_config": {
                "format": {"type": "json_schema", "schema": constrained_schema(schema)}
            },
        }
        request = Request(
            self._endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=bundle.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500] if exc.fp else ""
            raise GatewayTransportError(
                f"HTTP {exc.code} from the Messages API: {detail or exc.reason}",
                cause="transport_error",
                transient=exc.code in (408, 409, 429) or exc.code >= 500,
            ) from exc
        except URLError as exc:
            timed_out = isinstance(exc.reason, TimeoutError)
            raise GatewayTransportError(
                f"Messages API unreachable: {exc.reason}",
                cause="timeout" if timed_out else "transport_error",
                transient=True,
            ) from exc
        if payload.get("stop_reason") == "refusal":
            raise GatewayTransportError(
                "the model declined the request (stop_reason refusal)",
                cause="refusal",
                transient=False,
            )
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        usage = payload.get("usage", {})
        return RawReply(
            text=text,
            usage=Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
            model=payload.get("model"),
        )
