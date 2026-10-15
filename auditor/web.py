"""Every web call the auditor makes goes through here (the security review
prep of 2026-10-13): https to anywhere, or plain http only to this machine;
``file:`` and every other scheme are refused before a socket opens. The
engine holds the same rule in core/engine/web.py; the auditor keeps its own,
on purpose, because it imports nothing from core/."""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

_THIS_MACHINE = ("127.0.0.1", "localhost", "::1")


def open_url(
    url: str,
    *,
    timeout: float,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> Any:
    """Open ``url`` when the auditor may call it; ValueError otherwise."""
    parts = urlsplit(url)
    secure = parts.scheme == "https" and bool(parts.hostname)
    local = parts.scheme == "http" and parts.hostname in _THIS_MACHINE
    if not (secure or local):
        raise ValueError(f"the auditor calls https only (plain http only locally), not {url[:60]}")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)  # noqa: S310  (checked)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  (scheme checked above)
