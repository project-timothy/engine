"""The OpenAI-compatible adapter: the already-pinned ``openai`` client, used
exactly as the AP gateway extractor uses it today (``base_url``, key from an
environment variable, ``response_format={"type": "json_object"}``,
temperature 0). One adapter covers OpenAI, the Gemini compatibility
endpoint, OpenRouter, vLLM, Ollama, and the existing local gateway.

JSON mode has no schema slot, so the schema reaches the model as text: the
gateway already puts it in a system turn, and this adapter folds every
system turn into the one system message. Images travel as ``image_url``
data URLs; a PDF travels as the ``file`` content part (OpenAI's shape; a
compatibility endpoint that lacks it returns an error the gateway maps to a
transport failure, and the caller's text-extraction path stays the
fallback).

A PDF with NO text layer never gets that far: the engine renders it to page
images first (:mod:`core.llm.rasterize`, issue #296) and they travel as
ordinary ``image_url`` parts. This is the endpoint that needed it most. A
small open model behind vLLM or Ollama has no PDF reader at all, so before
this a scanned invoice on the local tier was not a poor extraction, it was no
extraction.

``build_client`` is module-level so a test can monkeypatch the transport;
the ``openai`` import stays inside it so the package loads lazily.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from core.llm.gateway import Attachment, PromptBundle, RawReply, Usage
from core.llm.rasterize import render_attachment

NO_AUTH_PLACEHOLDER = "sk-no-auth"


def build_client(*, base_url: str, api_key: str):
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key)


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _attachment_parts(attachment: Attachment) -> list[dict[str, Any]]:
    """The file, or the page images the engine rendered from it when it has no
    text layer."""
    rendered = render_attachment(attachment)
    if rendered is not None:
        parts: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": _data_url(rendered.mime, page)}}
            for page in rendered.pages
        ]
        parts.append({"type": "text", "text": rendered.note()})
        return parts
    url = _data_url(attachment.mime, attachment.path.read_bytes())
    if attachment.mime == "application/pdf":
        return [{"type": "file", "file": {"filename": attachment.path.name, "file_data": url}}]
    return [{"type": "image_url", "image_url": {"url": url}}]


def _messages(bundle: PromptBundle) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    system = bundle.system_text()
    if system:
        out.append({"role": "system", "content": system})
    pending = list(bundle.attachments)
    for turn in bundle.turns():
        if turn.role == "user" and pending:
            parts: list[dict[str, Any]] = []
            for attachment in pending:
                parts.extend(_attachment_parts(attachment))
            parts.append({"type": "text", "text": turn.content})
            out.append({"role": "user", "content": parts})
            pending = []
        else:
            out.append({"role": turn.role, "content": turn.content})
    return out


class OpenAICompatAdapter:
    name = "openai_compat"

    def __init__(self, *, base_url: str, api_key_env: str) -> None:
        self._base_url = base_url
        self._api_key_env = api_key_env

    def complete(self, bundle: PromptBundle, schema: dict[str, Any]) -> RawReply:
        client = build_client(
            base_url=self._base_url,
            api_key=os.environ.get(self._api_key_env, NO_AUTH_PLACEHOLDER),
        )
        response = client.chat.completions.create(
            model=bundle.model,
            messages=_messages(bundle),
            temperature=0,
            timeout=bundle.timeout_s,
            response_format={"type": "json_object"},
        )
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        return RawReply(
            text=text,
            usage=Usage(
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            ),
            model=getattr(response, "model", None),
        )
