"""Reading the copy of a document a rules file names (``[copy]``): what the
morning watch hashes (core/agents/policy) and what a pack's check reads its
quotes in (core/pack). Shared since chunk 8, its second user."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..engine import web
from ..llm.rasterize import pdf_pages
from . import Copy, Layer

COPY_MAX = 50 << 20
"""The largest copy read: a handbook, never a library."""


class CopyUnreadable(Exception):
    """The copy a rules file names could not be read into words."""


def read_copy(layer: Layer, copy: Copy) -> tuple[str, list[str]]:
    """The copy's sha256 and its words page by page: a PDF's text layer, or
    a text file's pages split at form feeds. Raises ``CopyUnreadable``."""
    try:
        if copy.url:
            with web.urlopen(copy.url, timeout=60) as resp:
                raw = resp.read(COPY_MAX + 1)
        else:
            raw = Path(copy.path).read_bytes()
    except (OSError, ValueError) as exc:
        raise CopyUnreadable(f"{layer.file}: {copy.url or copy.file} did not read ({exc})") from exc
    if len(raw) > COPY_MAX:
        raise CopyUnreadable(f"{layer.file}: {copy.url or copy.file} is over 50 MB")
    if raw.startswith(b"%PDF"):
        pages = pdf_pages(raw, name=copy.file or copy.url)
    else:
        try:
            pages = raw.decode("utf-8").split("\f")
        except UnicodeDecodeError:
            pages = []
    if not "".join(pages).strip():
        raise CopyUnreadable(f"{layer.file}: {copy.url or copy.file} has no words to read")
    return hashlib.sha256(raw).hexdigest(), pages
